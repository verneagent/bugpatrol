"""Validate and apply triage agent results."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from typing import Any, TypeGuard
from uuid import uuid4

from bugpatrol.clients import GitHubIssue, GitHubIssueComment, LarkMessengerClient
from bugpatrol.config import ProjectConfig
from bugpatrol.duplicate_evidence import (
    DuplicateEvidence,
    classify_duplicate_evidence,
    duplicate_evidence_payload,
    parse_duplicate_evidence,
)
from bugpatrol.fields import (
    AGENT_TRIAGE_STATUS_VALUES,
    NATIVE_ISSUE_TYPES,
    default_field_specs,
    validate_field_value,
)
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import (
    parse_intake_metadata,
    require_bugpatrol_managed_issue,
    resolve_reply_target,
)
from bugpatrol.lark import is_message_unreachable_error
from bugpatrol.triage_ownership import (
    TriageWriteSnapshot,
    TriageWriteSuperseded,
    WriteHistory,
    assignment_block_reason,
    assignment_changed_by_human,
    field_block_reason,
    latest_triage_run_id,
)


@dataclass(frozen=True)
class TriageResult:
    issue_type: str
    fields: dict[str, str]
    assignee: str
    comment_markdown: str
    blame_suggestion: str = ""
    suspected_owner: str = ""
    follow_up_questions: tuple[str, ...] = ()
    duplicate_of: int = 0
    corrected_title: str = ""
    duplicate_evidence: DuplicateEvidence | None = None


@dataclass(frozen=True)
class DuplicateRegression:
    """A recurrence verified against the affected build's effective fix."""

    issue_number: int
    issue_url: str
    closed_at: str = ""
    assignees: tuple[str, ...] = ()


@dataclass(frozen=True)
class DuplicateAssessment:
    classification: str
    may_close: bool
    regression: DuplicateRegression | None = None


@dataclass(frozen=True)
class TriageApplySummary:
    issue_type_written: bool
    fields_written: bool
    assignee_written: bool
    comment_added: bool
    duplicate_comment_skipped: bool
    result_fingerprint: str
    closed_as_duplicate: bool = False
    regression_reopened: int = 0


@dataclass(frozen=True)
class TriageRunStats:
    """Wall-clock cost of a triage run, for completion reporting."""

    duration_seconds: float = 0.0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


def format_run_stats(stats: TriageRunStats | None) -> str:
    """One-line 用时/模型/token summary, or "" when nothing to report."""
    if stats is None:
        return ""
    parts: list[str] = []
    if stats.duration_seconds > 0:
        parts.append(f"用时 {_format_duration(stats.duration_seconds)}")
    if stats.model:
        parts.append(f"模型 {stats.model}")
    if stats.input_tokens or stats.cached_input_tokens or stats.output_tokens:
        inp = f"输入{stats.input_tokens:,}"
        if stats.cached_input_tokens:
            inp += f"（+cache {stats.cached_input_tokens:,}）"
        parts.append(f"token {inp}/输出{stats.output_tokens:,}")
    return " · ".join(parts)


@dataclass(frozen=True)
class TriageFieldChange:
    field: str
    current: str
    proposed: str


@dataclass(frozen=True)
class TriageDryRunReport:
    issue_number: int
    issue_type: str
    assignee: str
    field_changes: tuple[TriageFieldChange, ...]
    comment_markdown: str
    result_fingerprint: str


TRIAGE_META_START = "<!-- BUGPATROL_TRIAGE_META"
TRIAGE_META_END = "BUGPATROL_TRIAGE_META -->"
TRIAGE_META_RE = re.compile(
    rf"{re.escape(TRIAGE_META_START)}\s*(.*?)\s*{re.escape(TRIAGE_META_END)}",
    re.DOTALL,
)
DUPLICATE_CLOSE_META = "BUGPATROL_DUPLICATE_CLOSE_META"
DUPLICATE_CLOSE_INTENT_META = "BUGPATROL_DUPLICATE_CLOSE_INTENT_META"
REGRESSION_FLAG_META = "BUGPATROL_REGRESSION_FLAG_META"
CORE_DUPLICATE_FIELDS = (
    "Priority",
    "Triage status",
    "Triage verdict",
    "Capability",
    "PRD status",
)


def parse_triage_result(data: dict[str, Any]) -> TriageResult:
    issue_type = _required_str(data, "issue_type")
    if issue_type not in NATIVE_ISSUE_TYPES:
        raise ValueError(f"invalid issue_type: {issue_type}")
    fields = {
        "Priority": _required_str(data, "priority"),
        "Triage status": _required_str(data, "triage_status"),
        "Triage verdict": _required_str(data, "triage_verdict"),
        "Platform": _required_str(data, "platform"),
        "Reproducibility": _required_str(data, "reproducibility"),
        "Other platforms": _required_str(data, "other_platforms"),
        "Capability": _required_str(data, "capability"),
        "Evidence": _required_str(data, "evidence"),
        "PRD status": _required_str(data, "prd_status"),
        "Triage confidence": _required_str(data, "triage_confidence"),
        "Owner reason": _required_str(data, "owner_reason"),
    }
    for field, value in fields.items():
        validate_field_value(field, value, default_field_specs())
    if fields["Triage status"] not in AGENT_TRIAGE_STATUS_VALUES:
        raise ValueError(
            f"triage_status must be a terminal state {AGENT_TRIAGE_STATUS_VALUES}, got: {fields['Triage status']}"
        )
    follow_up_questions = _optional_str_tuple(data, "follow_up_questions")
    if fields["Triage status"] == "Needs info" and not follow_up_questions:
        raise ValueError("Needs info triage requires follow_up_questions")
    if fields["Triage status"] != "Needs info":
        follow_up_questions = ()
    blame_suggestion = str(data.get("blame_suggestion") or "").strip()
    suspected_owner = str(data.get("suspected_owner") or "").strip().lstrip("@")
    duplicate_of = data.get("duplicate_of", 0)
    if not isinstance(duplicate_of, int) or isinstance(duplicate_of, bool) or duplicate_of < 0:
        raise ValueError(f"duplicate_of must be a non-negative integer, got: {duplicate_of!r}")
    if fields["Triage verdict"] == "重复" and duplicate_of == 0:
        raise ValueError("Triage verdict 重复 requires duplicate_of to name the existing issue")
    if duplicate_of > 0 and fields["Triage verdict"] != "重复":
        raise ValueError("duplicate_of is only allowed when Triage verdict is 重复")
    assignee = _required_str(data, "assignee").lstrip("@")
    comment = _required_str(data, "comment_markdown")
    corrected_title = _required_str(data, "corrected_title")
    return TriageResult(
        issue_type=issue_type,
        fields=fields,
        assignee=assignee,
        comment_markdown=comment,
        blame_suggestion=blame_suggestion,
        suspected_owner=suspected_owner,
        follow_up_questions=follow_up_questions,
        duplicate_of=duplicate_of,
        corrected_title=corrected_title,
        duplicate_evidence=parse_duplicate_evidence(data.get("duplicate_evidence")),
    )


# The intake auto-title carries a source marker prefix. A triage rewrite must
# preserve it, and the agent is told to draft without it — mirroring
# build_issue_title in intake_workflow.
SOURCE_TITLE_PREFIXES = ("[Lark] ", "[邮件] ")
TITLE_MAX_CHARS = 80


def corrected_issue_title(current: str, corrected: str) -> str:
    """Issue title to write after triage, or ``current`` when nothing changed.

    The agent drafts ``corrected_title`` without the source prefix, but the
    prefix (``[Lark] `` / ``[邮件] ``) is a source marker that must survive a
    rewrite. Returns ``current`` unchanged when ``corrected`` is empty or
    reduces to the current core, so repeated triage converges instead of
    churning the title.
    """
    corrected = corrected.strip()
    if not corrected:
        return current
    prefix = next((p for p in SOURCE_TITLE_PREFIXES if current.startswith(p)), "")
    for p in SOURCE_TITLE_PREFIXES:
        if corrected.startswith(p):
            corrected = corrected[len(p):].strip()
            break
    budget = TITLE_MAX_CHARS - len(prefix)
    if len(corrected) > budget:
        corrected = corrected[: budget - 3].rstrip() + "..."
    title = prefix + corrected
    return title if title != current else current


def apply_triage_result(
    *,
    repo: str,
    issue_number: int,
    config: ProjectConfig,
    result: TriageResult,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None = None,
    run_stats: TriageRunStats | None = None,
    branch_note: str = "",
    write_snapshot: TriageWriteSnapshot | None = None,
    run_id: str = "",
) -> TriageApplySummary:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)
    if result.duplicate_of == issue_number:
        raise ValueError(f"duplicate_of must reference a different issue, got #{result.duplicate_of}")
    assessment = assess_duplicate(
        repo=repo,
        duplicate_of=result.duplicate_of,
        github=github,
        evidence=result.duplicate_evidence,
    )
    regression = assessment.regression if assessment is not None else None
    duplicate_classification = assessment.classification if assessment is not None else ""
    regression_note = render_regression_note(regression)
    duplicate_note = render_duplicate_note(duplicate_classification)
    lifecycle_suggestion = ""
    lifecycle_note = ""
    if write_snapshot is None and assessment is not None:
        if regression is not None:
            lifecycle_suggestion = f"Review reopening #{regression.issue_number} and closing this report as a duplicate."
        elif assessment.may_close:
            lifecycle_suggestion = f"Review closing this report as a duplicate of #{result.duplicate_of}."
        elif issue.state == "closed":
            lifecycle_suggestion = "Review whether to reopen this report for manual verification."
        else:
            lifecycle_suggestion = "Keep this report available for manual verification."
        lifecycle_note = (
            "No plan snapshot. Lifecycle suggestion only: " + lifecycle_suggestion
            + " No issue open/closed state was changed. Use run-triage with a prepared plan before applying lifecycle changes."
        )
        regression_note = ""
        duplicate_note = lifecycle_note
    if assessment is not None and not assessment.may_close:
        questions = result.follow_up_questions or (
            "请核实报告环境、原始发生时间、实际 build 与有效修复，并补充可复核的证据引用。",
        )
        result = replace(
            result, fields={**result.fields, "Triage status": "Needs info"},
            follow_up_questions=questions,
        )
    fingerprint = triage_result_fingerprint(result)
    decision_key = triage_decision_key(result)
    existing_comments = github.list_issue_comments(repo=repo, issue_number=issue_number)

    def check_run() -> None:
        comments = github.list_issue_comments(repo=repo, issue_number=issue_number)
        if latest_triage_run_id(comments) != run_id:
            raise TriageWriteSuperseded("The triage run no longer owns issue writeback.")

    check_run()
    restore_report = (
        write_snapshot is not None and assessment is not None and not assessment.may_close and issue.state == "closed"
        and (
            _matching_duplicate_close_receipt(issue, existing_comments, result.duplicate_of)
            or _matching_duplicate_close_intent(
                issue, existing_comments, result.duplicate_of, repo=repo, github=github,
            )
        )
    )
    if write_snapshot is not None and assessment is not None and not assessment.may_close and issue.state == "closed" and not restore_report:
        duplicate_note += " 当前关闭操作缺少匹配的 bot receipt，保留其关闭状态，请人工核实是否需要重新打开。"
    existing_field_values = issue_fields.get_issue_field_values(
        repo=repo,
        issue_number=issue_number,
    )
    applied: dict[str, str] = {}
    suggestions: dict[str, str] = {}
    if lifecycle_suggestion:
        suggestions["lifecycle"] = lifecycle_suggestion
    warnings: list[str] = []
    assignee_written = False
    owner_fields_allowed = False
    assignment_history = WriteHistory()

    def fresh_state() -> tuple[GitHubIssue, dict[str, str], WriteHistory]:
        if write_snapshot is None:
            return issue, existing_field_values, WriteHistory()
        return (
            github.get_issue(repo=repo, issue_number=issue_number),
            issue_fields.get_issue_field_values(repo=repo, issue_number=issue_number),
            github.get_issue_write_history(repo=repo, issue_number=issue_number),
        )

    current, current_fields, history = fresh_state()
    reason = assignment_block_reason(write_snapshot, current, history)
    if not result.duplicate_of and reason is None:
        check_run()
        assignment_history = history
        github.add_assignee(repo=repo, issue_number=issue_number, assignee=result.assignee)
        assignee_written = True
        current, current_fields, history = fresh_state()
        owner_fields_allowed = (
            set(current.assignees) == {result.assignee}
            and not assignment_changed_by_human(assignment_history, history)
        )
        if not owner_fields_allowed:
            warnings.append("Assignment changed during writeback; review the current assignees. No assignee was removed.")
    elif not result.duplicate_of:
        if set(current.assignees) != {result.assignee}:
            suggestions["assignee"] = result.assignee
            warnings.append(reason or "Assignment was preserved.")

    issue_type_written = False
    for name, proposed in (("issue_type", result.issue_type), ("title", corrected_issue_title(issue.title, result.corrected_title))):
        current, current_fields, history = fresh_state()
        actual = current.issue_type if name == "issue_type" else current.title
        if actual == proposed:
            continue
        blocked = field_block_reason(write_snapshot, current, current_fields, history, name)
        if blocked is not None:
            suggestions[name] = proposed
            continue
        check_run()
        if name == "issue_type":
            github.set_issue_type(repo=repo, issue_number=issue_number, issue_type=proposed)
            issue_type_written = True
        else:
            github.update_issue_title(repo=repo, issue_number=issue_number, title=proposed)
        applied[name] = proposed

    current, current_fields, history = fresh_state()
    owner_fields_allowed = (
        owner_fields_allowed and set(current.assignees) == {result.assignee}
        and not assignment_changed_by_human(assignment_history, history)
    )
    values: dict[str, str] = {}
    for field, proposed in triage_field_values_for_write(result, config=config).items():
        if field == "Triage status":
            values[field] = proposed
            continue
        actual_name = config.issue_field_names.get(field, field)
        if current_fields.get(actual_name, "") == proposed:
            continue
        if field in ("Owner", "Owner reason") and not owner_fields_allowed:
            suggestions[field] = proposed
            continue
        if field_block_reason(write_snapshot, current, current_fields, history, actual_name) is not None:
            suggestions[field] = proposed
        else:
            values[field] = proposed
    if values:
        check_run()
        issue_fields.add_issue_field_values(
            repo=repo, issue_number=issue_number, values={**values}, config=config,
        )
        applied.update(values)
    ownership_key = _sha256_json({
        "run_id": run_id, "proposal": fingerprint, "suggestions": suggestions,
        "assignees": current.assignees,
    })
    duplicate = _has_applied_triage_decision(
        comments=existing_comments, fingerprint=fingerprint, decision_key=decision_key,
        result=result, config=config, existing_field_values=existing_field_values,
        ownership_key=ownership_key,
    )
    write_note = ""
    if suggestions:
        write_note = "Preserved human or unverified metadata. Suggestions: " + json.dumps(suggestions, ensure_ascii=False, sort_keys=True)
    if warnings:
        write_note += " " + " ".join(warnings)
    if restore_report:
        current = github.get_issue(repo=repo, issue_number=issue_number)
        if _same_duplicate_closure(issue, current):
            check_run()
            github.reopen_issue(repo=repo, issue_number=issue_number)
    if not duplicate:
        check_run()
        if lark is not None and result.fields["Triage status"] == "Needs info":
            _send_lark_follow_up(
                repo=repo,
                issue_number=issue_number,
                result=result,
                github=github,
                lark=lark,
                branch_note=branch_note,
                lifecycle_note=lifecycle_note,
            )
        elif lark is not None:
            _send_lark_triage_summary(
                repo=repo,
                issue_number=issue_number,
                result=result,
                github=github,
                lark=lark,
                config=config,
                run_stats=run_stats,
                branch_note=branch_note,
                regression_note=regression_note,
                duplicate_classification=duplicate_classification,
                assignee_applied=assignee_written,
                lifecycle_note=lifecycle_note,
            )
        check_run()
        github.add_issue_comment(
            repo=repo,
            issue_number=issue_number,
            body=append_triage_metadata(
                _with_runner_attribution(
                    render_triage_comment(
                        result, branch_note=branch_note, regression_note=regression_note,
                        duplicate_note=duplicate_note,
                        write_note=write_note,
                    ),
                    run_stats=run_stats,
                ),
                {
                    "version": 1,
                    "issue": issue_number,
                    "result_fingerprint": fingerprint,
                    "decision_key": decision_key,
                    "duplicate_of": result.duplicate_of if write_snapshot is not None and assessment is not None and assessment.may_close else 0,
                    "related_issue": result.duplicate_of,
                    "duplicate_classification": duplicate_classification,
                    "duplicate_evidence": duplicate_evidence_payload(result.duplicate_evidence),
                    "regression_of": regression.issue_number if write_snapshot is not None and regression is not None else 0,
                    "verdict": result.fields.get("Triage verdict", ""),
                    "triage_status": result.fields.get("Triage status", ""),
                    "run_id": run_id,
                    "ownership_key": ownership_key,
                    "applied_fields": applied,
                    "suggested_fields": suggestions,
                    "assignee_written": assignee_written,
                    "blame_suggestion": result.blame_suggestion,
                    "suspected_owner": result.suspected_owner,
                },
            ),
        )
    elif lark is not None:
        # Decision unchanged: don't spam a duplicate comment/summary, but still
        # ping the topic so the reporter knows the run completed (didn't hang).
        _send_lark_triage_unchanged(
            repo=repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            run_stats=run_stats,
        )
    closed_as_duplicate = False
    regression_reopened = 0
    if result.duplicate_of and write_snapshot is not None:
        check_run()
        if regression is not None:
            _flag_duplicate_regression(
                repo=repo,
                regression=regression,
                duplicate_issue_number=issue_number,
                duplicate_issue_url=issue.url,
                github=github,
                issue_fields=issue_fields,
                lark=lark,
                config=config,
            )
            regression_reopened = regression.issue_number
        if assessment is not None and assessment.may_close:
            current = github.get_issue(repo=repo, issue_number=issue_number)
            if current.state != "closed":
                github.add_issue_comment(
                    repo=repo, issue_number=issue_number,
                    body=_append_operation_metadata(
                        "BugPatrol duplicate close intent.", DUPLICATE_CLOSE_INTENT_META,
                        {"issue": issue_number, "duplicate_of": result.duplicate_of,
                         "operation": uuid4().hex, "classification": assessment.classification,
                         "result_fingerprint": fingerprint},
                    ),
                )
                github.close_issue_as_duplicate(
                    repo=repo, issue_number=issue_number, duplicate_of=result.duplicate_of,
                )
                _record_duplicate_close(
                    repo=repo, issue_number=issue_number, duplicate_of=result.duplicate_of,
                    classification=assessment.classification, fingerprint=fingerprint, github=github,
                )
                closed_as_duplicate = True
            else:
                if (
                    not _matching_duplicate_close_receipt(current, existing_comments, result.duplicate_of)
                    and _matching_duplicate_close_intent(
                        current, existing_comments, result.duplicate_of, repo=repo, github=github,
                    )
                ):
                    _record_duplicate_close(
                        repo=repo, issue_number=issue_number, duplicate_of=result.duplicate_of,
                        classification=assessment.classification, fingerprint=fingerprint, github=github,
                    )
                closed_as_duplicate = current.state_reason == "duplicate"
    return TriageApplySummary(
        issue_type_written=issue_type_written,
        fields_written=bool(values),
        assignee_written=assignee_written,
        comment_added=not duplicate,
        duplicate_comment_skipped=duplicate,
        result_fingerprint=fingerprint,
        closed_as_duplicate=closed_as_duplicate,
        regression_reopened=regression_reopened,
    )


def _flag_duplicate_regression(
    *,
    repo: str,
    regression: DuplicateRegression,
    duplicate_issue_number: int,
    duplicate_issue_url: str,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None,
    config: ProjectConfig,
) -> None:
    """Prepare retriable state before opening the original to the watcher."""
    issue_fields.add_issue_field_values(
        repo=repo,
        issue_number=regression.issue_number,
        values={"Triage status": "Pending"},
        config=config,
    )
    operation = f"{duplicate_issue_number}:{regression.closed_at}"
    comments = github.list_issue_comments(repo=repo, issue_number=regression.issue_number)
    already_flagged = any(
        comment.author_type == "Bot"
        and (_operation_metadata(comment.body, REGRESSION_FLAG_META) or {}).get("operation") == operation
        for comment in comments
    )
    if not already_flagged:
        if lark is not None:
            send_intake_topic_message(
                repo=repo, issue_number=regression.issue_number, github=github, lark=lark,
                text=render_regression_lark_message(
                    regression=regression, duplicate_issue_number=duplicate_issue_number,
                    duplicate_issue_url=duplicate_issue_url,
                    assignee_open_ids=config.lark.user_open_ids or {},
                ),
            )
        github.add_issue_comment(
            repo=repo, issue_number=regression.issue_number,
            body=_append_operation_metadata(
                render_regression_flag_comment(
                    regression=regression, duplicate_issue_number=duplicate_issue_number,
                    duplicate_issue_url=duplicate_issue_url,
                ), REGRESSION_FLAG_META, {"operation": operation},
            ),
        )
    github.reopen_issue(repo=repo, issue_number=regression.issue_number)


def _same_duplicate_closure(previous: GitHubIssue, current: GitHubIssue) -> bool:
    return (
        current.state == "closed" and current.state_reason == "duplicate"
        and current.closed_by_type == "Bot" and bool(current.closed_at)
        and (current.closed_at, current.closed_by) == (previous.closed_at, previous.closed_by)
    )


def _matching_duplicate_close_receipt(
    issue: GitHubIssue, comments: tuple[GitHubIssueComment, ...], duplicate_of: int,
) -> bool:
    if issue.state_reason != "duplicate" or issue.closed_by_type != "Bot" or not issue.closed_at:
        return False
    for comment in comments:
        if comment.author_type != "Bot" or comment.author != issue.closed_by:
            continue
        metadata = _operation_metadata(comment.body, DUPLICATE_CLOSE_META)
        if (
            _valid_close_metadata(metadata)
            and metadata.get("issue") == issue.number
            and metadata.get("duplicate_of") == duplicate_of
            and metadata.get("closed_at") == issue.closed_at
            and metadata.get("closed_by") == issue.closed_by
        ):
            return True
    return False


def _matching_duplicate_close_intent(
    issue: GitHubIssue, comments: tuple[GitHubIssueComment, ...], duplicate_of: int,
    *, repo: str, github: GitHubCliIssuesClient,
) -> bool:
    if not _same_duplicate_closure(issue, issue):
        return False
    for comment in reversed(comments):
        if comment.author_type != "Bot" or comment.author != issue.closed_by:
            continue
        intent = _operation_metadata(comment.body, DUPLICATE_CLOSE_INTENT_META)
        if not _valid_close_intent(intent) or intent["issue"] != issue.number or intent["duplicate_of"] != duplicate_of:
            continue
        timeline = github.list_issue_timeline(repo=repo, issue_number=issue.number)
        boundary = next((
            index for index, event in enumerate(timeline)
            if event.get("event") == "commented" and str(event.get("id")) == comment.id
        ), None)
        if boundary is None:
            return False
        lifecycle = [event for event in timeline[boundary + 1:] if event.get("event") in ("closed", "reopened")]
        if len(lifecycle) != 1 or lifecycle[0].get("event") != "closed":
            return False
        event = lifecycle[0]
        actor = event.get("actor")
        return (
            isinstance(actor, dict) and actor.get("type") == "Bot"
            and actor.get("login") == issue.closed_by and event.get("created_at") == issue.closed_at
        )
    return False


def _record_duplicate_close(
    *, repo: str, issue_number: int, duplicate_of: int,
    classification: str, fingerprint: str, github: GitHubCliIssuesClient,
) -> None:
    closed = github.get_issue(repo=repo, issue_number=issue_number)
    if (
        closed.state != "closed" or closed.state_reason != "duplicate"
        or closed.closed_by_type != "Bot" or not closed.closed_at
    ):
        return
    try:
        github.add_issue_comment(
            repo=repo, issue_number=issue_number,
            body=_append_operation_metadata(
                "BugPatrol duplicate close receipt.", DUPLICATE_CLOSE_META,
                {"issue": issue_number, "duplicate_of": duplicate_of,
                 "closed_at": closed.closed_at, "closed_by": closed.closed_by,
                 "classification": classification, "result_fingerprint": fingerprint},
            ),
        )
    except Exception:
        comments = github.list_issue_comments(repo=repo, issue_number=issue_number)
        if _matching_duplicate_close_receipt(closed, comments, duplicate_of):
            return
        if _matching_duplicate_close_intent(closed, comments, duplicate_of, repo=repo, github=github):
            current = github.get_issue(repo=repo, issue_number=issue_number)
            if _same_duplicate_closure(closed, current):
                github.reopen_issue(repo=repo, issue_number=issue_number)
        raise


def _append_operation_metadata(body: str, marker: str, metadata: dict[str, Any]) -> str:
    return f"{body}\n\n<!-- {marker}\n{json.dumps(metadata, sort_keys=True)}\n{marker} -->"


def _operation_metadata(body: str, marker: str) -> dict[str, Any] | None:
    match = re.search(rf"<!-- {marker}\s*(.*?)\s*{marker} -->", body, re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _valid_close_metadata(metadata: dict[str, Any] | None) -> TypeGuard[dict[str, Any]]:
    return (
        metadata is not None
        and type(metadata.get("issue")) is int and metadata["issue"] > 0
        and type(metadata.get("duplicate_of")) is int and metadata["duplicate_of"] > 0
        and metadata.get("classification") in ("open_duplicate", "pre_fix_duplicate", "old_build_duplicate", "confirmed_regression")
        and isinstance(metadata.get("result_fingerprint"), str)
        and re.fullmatch(r"[a-f0-9]{64}", metadata["result_fingerprint"]) is not None
    )


def _valid_close_intent(metadata: dict[str, Any] | None) -> TypeGuard[dict[str, Any]]:
    return (
        _valid_close_metadata(metadata) and metadata is not None
        and isinstance(metadata.get("operation"), str)
        and re.fullmatch(r"[a-f0-9]{32}", metadata["operation"]) is not None
    )


def is_duplicate_close_bookkeeping(body: str) -> bool:
    receipt = _operation_metadata(body, DUPLICATE_CLOSE_META)
    return (
        _valid_close_metadata(receipt) and receipt is not None
        and isinstance(receipt.get("closed_by"), str) and bool(receipt["closed_by"])
        and isinstance(receipt.get("closed_at"), str) and bool(receipt["closed_at"])
    ) or _valid_close_intent(_operation_metadata(body, DUPLICATE_CLOSE_INTENT_META))


def build_triage_dry_run_report(
    *,
    repo: str,
    issue_number: int,
    config: ProjectConfig,
    result: TriageResult,
    issue_fields: GitHubIssueFieldsClient,
) -> TriageDryRunReport:
    live_values = issue_fields.get_issue_field_values(repo=repo, issue_number=issue_number)
    changes: list[TriageFieldChange] = []
    for logical_name, proposed in triage_field_values_for_write(result, config=config).items():
        github_name = config.issue_field_names.get(logical_name, logical_name)
        current = live_values.get(github_name, "")
        if current != proposed:
            changes.append(
                TriageFieldChange(
                    field=logical_name,
                    current=current,
                    proposed=proposed,
                )
            )
    return TriageDryRunReport(
        issue_number=issue_number,
        issue_type=result.issue_type,
        assignee=result.assignee,
        field_changes=tuple(changes),
        comment_markdown=render_triage_comment(result),
        result_fingerprint=triage_result_fingerprint(result),
    )


def triage_result_fingerprint(result: TriageResult) -> str:
    return _sha256_json(triage_decision_payload(result))


def triage_decision_key(result: TriageResult) -> str:
    return _sha256_json(triage_decision_payload(result))


def triage_decision_payload(result: TriageResult) -> dict[str, object]:
    payload = {
        "issue_type": result.issue_type,
        "fields": {field: result.fields.get(field, "") for field in CORE_DUPLICATE_FIELDS},
        "assignee": result.assignee,
        "follow_up_questions": result.follow_up_questions,
        "duplicate_of": result.duplicate_of,
    }
    if result.duplicate_of:
        payload["duplicate_evidence"] = duplicate_evidence_payload(result.duplicate_evidence)
    return payload


def _sha256_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def append_triage_metadata(comment_markdown: str, metadata: dict[str, Any]) -> str:
    return (
        f"{comment_markdown.rstrip()}\n\n"
        f"{TRIAGE_META_START}\n"
        f"{json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)}\n"
        f"{TRIAGE_META_END}"
    )


def _with_runner_attribution(
    comment_markdown: str,
    run_stats: TriageRunStats | None = None,
) -> str:
    footer: list[str] = []
    runner = triage_runner_name()
    if runner:
        footer.append(f"分诊执行机：`{runner}`")
    stats_line = format_run_stats(run_stats)
    if stats_line:
        footer.append(stats_line)
    if not footer:
        return comment_markdown
    return f"{comment_markdown.rstrip()}\n\n> " + "\n> ".join(footer)


def triage_runner_name() -> str:
    """Name of the CI runner executing this triage, for attribution."""
    return os.environ.get("BUGPATROL_RUNNER_NAME") or os.environ.get("RUNNER_NAME", "")


# owner_reason values that mean the assignee was inferred rather than matched
# to a concrete CODEOWNERS path rule (or explicitly named by a human). When a
# code bug's owner is only inferred, the touched module has no CODEOWNERS
# coverage — surface that so the gap gets fixed instead of silently
# mis-assigning future bugs in the same area (#3946: a git-history guess sent a
# voice-call bug to the wrong owner because call/ had no CODEOWNERS rule).
INFERRED_OWNER_REASONS = frozenset({"Git history", "Capability fallback"})


def codeowners_coverage_caveat(result: TriageResult) -> str:
    """Warning line when a code bug's owner was inferred, not CODEOWNERS-matched.

    Returns "" when the owner is authoritative (CODEOWNERS path match, or a
    human's Lark @mention / Manual instruction) or when there is no owner.
    """
    if result.fields.get("Triage verdict", "") != "代码 Bug":
        return ""
    if not result.assignee:
        return ""
    if result.fields.get("Owner reason", "") not in INFERRED_OWNER_REASONS:
        return ""
    return (
        f"⚠️ 该模块未被 CODEOWNERS 覆盖，负责人 @{result.assignee} 由线索推断"
        f"（{result.fields.get('Owner reason', '')}），请在 CODEOWNERS 补对应路径规则，"
        "否则同模块的后续 bug 会继续被错误归属。"
    )


def assess_duplicate(
    *,
    repo: str,
    duplicate_of: int,
    github: GitHubCliIssuesClient,
    evidence: DuplicateEvidence | None,
) -> DuplicateAssessment | None:

    if not duplicate_of:
        return None
    original = github.get_issue(repo=repo, issue_number=duplicate_of)
    if original.state == "open":
        return DuplicateAssessment("open_duplicate", True)
    if original.state != "closed":
        return DuplicateAssessment("needs_verification", False)
    classification = classify_duplicate_evidence(evidence)
    if original.state_reason in ("not_planned", "duplicate"):
        classification = "needs_verification"
    if classification != "confirmed_regression":
        return DuplicateAssessment(
            classification, classification in ("pre_fix_duplicate", "old_build_duplicate")
        )
    if parse_intake_metadata(original.body or "") is None:
        return DuplicateAssessment("needs_verification", False)
    return DuplicateAssessment(
        classification,
        True,
        DuplicateRegression(
            issue_number=original.number,
            issue_url=original.url,
            closed_at=original.closed_at,
            assignees=original.assignees,
        ),
    )


def render_duplicate_note(classification: str) -> str:
    return {
        "delivery_incomplete": "交付未完成：已确认该环境缺少有效修复，保留当前报告跟踪交付，不判为新回归。",
        "pre_fix_duplicate": "修复前发生的重复报告：已核对有效交付时间，不重新打开原 issue。",
        "old_build_duplicate": "旧版本重复报告：已确认实际 build 不含修复，当前环境已有有效交付。",
        "needs_verification": "证据不足或冲突：保留当前报告，关联原 issue，等待人工核实环境、发生时间、实际 build 与有效修复。",
    }.get(classification, "")


def render_regression_note(regression: DuplicateRegression | None) -> str:
    if regression is None:
        return ""
    return (
        f"⚠️ 已确认回归（regression）：已核对实际 build 含有仍有效的修复，"
        f"且问题发生在有效交付后，将重新打开原 issue #{regression.issue_number} 跟踪。"
    )


def render_regression_flag_comment(
    *,
    regression: DuplicateRegression,
    duplicate_issue_number: int,
    duplicate_issue_url: str,
) -> str:
    closed_note = f"（关闭于 {regression.closed_at}）" if regression.closed_at else ""
    return "\n".join(
        [
            "## ⚠️ 已确认回归（regression）",
            "",
            f"本 issue 此前已作为「已修复」关闭{closed_note}，"
            f"但新上报的 [#{duplicate_issue_number}]({duplicate_issue_url}) 被分诊判定为同一问题。",
            "已核对报告环境、原始发生时间和实际 build，确认实际 build 包含仍有效的修复，"
            "且同一问题发生于有效交付之后，将重新打开本 issue。",
            "",
            "请确认：修复是否仍在代码里、是否需要补回归测试、还是这是一条新的触发路径。",
        ]
    )


def render_regression_lark_message(
    *,
    regression: DuplicateRegression,
    duplicate_issue_number: int,
    duplicate_issue_url: str,
    assignee_open_ids: dict[str, str] | None = None,
) -> str:
    mentions = " ".join(
        f'<at user_id="{open_id}">{login}</at>'
        for login in regression.assignees
        for open_id in ((assignee_open_ids or {}).get(login, ""),)
        if open_id
    )
    head = f"{mentions} " if mentions else ""
    return "\n".join(
        [
            f"{head}⚠️ 已确认回归（regression）：本 issue "
            f"[#{regression.issue_number}]({regression.issue_url}) 曾以「已修复」关闭，"
            f"但新上报的 [#{duplicate_issue_number}]({duplicate_issue_url}) 是同一问题。",
            "已核对实际 build 的有效修复与原始发生时间，将重新打开本 issue 跟踪未覆盖的触发路径。",
        ]
    )


def render_triage_comment(
    result: TriageResult,
    *,
    branch_note: str = "",
    regression_note: str = "",
    duplicate_note: str = "",
    write_note: str = "",
) -> str:
    body = result.comment_markdown.rstrip()
    if duplicate_note and result.follow_up_questions:
        body += "\n\n" + "\n".join(result.follow_up_questions)
    if result.blame_suggestion or result.suspected_owner:
        if "Blame" not in body and "归因" not in body:
            parts = []
            if result.suspected_owner:
                parts.append(f"疑似引入人（Owner）：{result.suspected_owner}")
            if result.blame_suggestion:
                parts.append(f"归因线索：{result.blame_suggestion}")
            body = f"{body}\n\n" + "\n".join(parts)
    prefix_lines = []
    if write_note:
        prefix_lines.append(f"> {write_note}")
    if regression_note:
        prefix_lines.append(f"> {regression_note}")
    if duplicate_note:
        prefix_lines.append(f"> {duplicate_note}")
    if branch_note:
        prefix_lines.append(f"> {branch_note}")
    caveat = codeowners_coverage_caveat(result)
    if caveat:
        prefix_lines.append(f"> {caveat}")
    if prefix_lines:
        body = "\n".join(prefix_lines) + f"\n\n{body}"
    return body


def triage_field_values_for_write(
    result: TriageResult,
    *,
    config: ProjectConfig,
) -> dict[str, str]:
    values = dict(result.fields)
    if result.suspected_owner and "Owner" in config.issue_field_names:
        values["Owner"] = result.suspected_owner
    return values


def parse_triage_metadata(comment_body: str) -> dict[str, Any] | None:
    match = TRIAGE_META_RE.search(comment_body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _has_applied_triage_decision(
    *,
    comments: tuple[GitHubIssueComment, ...],
    fingerprint: str,
    decision_key: str,
    result: TriageResult,
    config: ProjectConfig,
    existing_field_values: dict[str, str],
    ownership_key: str = "",
) -> bool:
    has_prior_triage = False
    for comment in comments:
        if comment.author_type != "Bot":
            continue
        metadata = parse_triage_metadata(comment.body)
        if ownership_key and (metadata is None or metadata.get("ownership_key") != ownership_key):
            continue
        if result.duplicate_of and (
            metadata is None or not metadata.get("duplicate_classification")
            or metadata.get("duplicate_evidence") != duplicate_evidence_payload(result.duplicate_evidence)
        ):
            continue
        if metadata is not None and metadata.get("result_fingerprint") == fingerprint:
            return True
        if metadata is not None and metadata.get("decision_key") == decision_key:
            return True
        if metadata is not None and _triage_comment_matches_decision(comment.body, result):
            return True
        if metadata is not None:
            has_prior_triage = True
    if result.duplicate_of or ownership_key:
        return False
    if not has_prior_triage:
        return False
    return all(
        existing_field_values.get(config.issue_field_names.get(field, field), "")
        == result.fields.get(field, "")
        for field in CORE_DUPLICATE_FIELDS
    )


def _triage_comment_matches_decision(comment_body: str, result: TriageResult) -> bool:
    if result.fields.get("Triage status") == "Needs info":
        return False
    if "## Triage" not in comment_body:
        return False
    required_tokens = (
        result.fields.get("Triage verdict", ""),
        f"优先级 {result.fields.get('Priority', '')}",
        f"@{result.assignee}",
    )
    return all(token and token in comment_body for token in required_tokens)


def _send_lark_follow_up(
    *,
    repo: str,
    issue_number: int,
    result: TriageResult,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient,
    branch_note: str = "",
    lifecycle_note: str = "",
) -> None:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    metadata = parse_intake_metadata(issue.body or "") or {}
    _reply_to_intake_topic(
        issue_body=issue.body or "",
        lark=lark,
        text=render_needs_info_lark_message(
            issue_number=issue_number,
            issue_url=issue.url,
            questions=result.follow_up_questions,
            reporter_open_id=_metadata_str(metadata, "reporter_open_id"),
            branch_note=branch_note,
            lifecycle_note=lifecycle_note,
        ),
    )


def _send_lark_triage_summary(
    *,
    repo: str,
    issue_number: int,
    result: TriageResult,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient,
    config: ProjectConfig,
    run_stats: TriageRunStats | None = None,
    branch_note: str = "",
    regression_note: str = "",
    duplicate_classification: str = "",
    assignee_applied: bool | None = None,
    lifecycle_note: str = "",
) -> None:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    _reply_to_intake_topic(
        issue_body=issue.body or "",
        lark=lark,
        text=render_triage_summary_lark_message(
            issue_number=issue_number,
            issue_url=issue.url,
            result=result,
            assignee_open_id=(config.lark.user_open_ids or {}).get(result.assignee, ""),
            runner_name=triage_runner_name(),
            run_stats=run_stats,
            branch_note=branch_note,
            regression_note=regression_note,
            duplicate_classification=duplicate_classification,
            assignee_applied=assignee_applied,
            lifecycle_note=lifecycle_note,
        ),
    )


def _send_lark_triage_unchanged(
    *,
    repo: str,
    issue_number: int,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient,
    run_stats: TriageRunStats | None = None,
) -> None:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    _reply_to_intake_topic(
        issue_body=issue.body or "",
        lark=lark,
        text=render_triage_unchanged_lark_message(
            issue_number=issue_number,
            issue_url=issue.url,
            runner_name=triage_runner_name(),
            run_stats=run_stats,
        ),
    )


def render_triage_unchanged_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    runner_name: str = "",
    run_stats: TriageRunStats | None = None,
) -> str:
    lines = [f"分诊完成，结论无变化，GitHub issue [#{issue_number}]({issue_url})"]
    if runner_name:
        lines.append(f"分诊执行机：{runner_name}")
    stats_line = format_run_stats(run_stats)
    if stats_line:
        lines.append(stats_line)
    return "\n".join(lines)


def send_intake_topic_message(
    *,
    repo: str,
    issue_number: int,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient,
    text: str,
) -> None:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    _reply_to_intake_topic(issue_body=issue.body or "", lark=lark, text=text)


def _reply_to_intake_topic(
    *,
    issue_body: str,
    lark: LarkMessengerClient,
    text: str,
) -> None:
    metadata = parse_intake_metadata(issue_body)
    if metadata is None:
        return
    chat_id, message_id = resolve_reply_target(metadata)
    if not chat_id or not message_id:
        return
    try:
        lark.reply_to_message(chat_id=chat_id, message_id=message_id, text=text)
    except Exception as error:
        # The GitHub side of the run is already applied; a source message that
        # is gone (recalled, or its thread deleted) must not fail the run over a
        # best-effort Lark notification.
        if not is_message_unreachable_error(error):
            raise


def render_triage_summary_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    result: TriageResult,
    assignee_open_id: str = "",
    runner_name: str = "",
    run_stats: TriageRunStats | None = None,
    branch_note: str = "",
    regression_note: str = "",
    duplicate_classification: str = "",
    assignee_applied: bool | None = None,
    lifecycle_note: str = "",
) -> str:
    lines = [f"分诊完成，GitHub issue [#{issue_number}]({issue_url})"]
    if branch_note:
        lines.append(branch_note)
    if regression_note:
        lines.append(regression_note)
    if result.duplicate_of:
        duplicate_url = f"{issue_url.rsplit('/', 1)[0]}/{result.duplicate_of}"
        classification = duplicate_classification or classify_duplicate_evidence(result.duplicate_evidence)
        if lifecycle_note:
            lines.append(f"Related issue: [#{result.duplicate_of}]({duplicate_url})")
            lines.append(lifecycle_note)
        elif classification in ("open_duplicate", "pre_fix_duplicate", "confirmed_regression", "old_build_duplicate"):
            lines.append(f"结论：重复。重复于 [#{result.duplicate_of}]({duplicate_url})")
        else:
            lines.append(f"结论：关联 [#{result.duplicate_of}]({duplicate_url})，保留当前报告。")
        note = render_duplicate_note(classification)
        if note and not lifecycle_note:
            lines.append(note)
    else:
        assignee = result.assignee
        if assignee and assignee_open_id:
            assignee = f'<at user_id="{assignee_open_id}">{result.assignee}</at>'
        for label, value in (
            ("结论", result.fields.get("Triage verdict", "")),
            ("状态", result.fields.get("Triage status", "")),
            ("优先级", result.fields.get("Priority", "")),
            ("建议负责人" if assignee_applied is False else "负责人", assignee),
        ):
            if value:
                lines.append(f"{label}：{value}")
        caveat = codeowners_coverage_caveat(result)
        if caveat:
            lines.append(caveat)
    if runner_name:
        lines.append(f"分诊执行机：{runner_name}")
    stats_line = format_run_stats(run_stats)
    if stats_line:
        lines.append(stats_line)
    return "\n".join(lines)


def render_needs_info_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    questions: tuple[str, ...],
    reporter_open_id: str = "",
    branch_note: str = "",
    lifecycle_note: str = "",
) -> str:
    prefix = f'<at user_id="{reporter_open_id}"></at> ' if reporter_open_id else ""
    lines = [f"{prefix}需要补充信息，GitHub issue [#{issue_number}]({issue_url})"]
    if branch_note:
        lines.append(branch_note)
    if lifecycle_note:
        lines.append(lifecycle_note)
    lines.append("")
    lines.extend(f"{index}. {question}" for index, question in enumerate(questions, start=1))
    return "\n".join(lines)


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing string field: {key}")
    return value


def _optional_str_tuple(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{key} must be a string array")
    return tuple(value)


def _metadata_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""
