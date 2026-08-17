"""Validate and apply triage agent results."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from bugpatrol.clients import GitHubIssueComment, LarkMessengerClient
from bugpatrol.config import ProjectConfig
from bugpatrol.fields import AGENT_TRIAGE_STATUS_VALUES, NATIVE_ISSUE_TYPES, default_field_specs, validate_field_value
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import parse_intake_metadata, require_bugpatrol_managed_issue
from bugpatrol.lark import is_message_unreachable_error


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


@dataclass(frozen=True)
class DuplicateRegression:
    """The issue a duplicate points at was already closed as fixed.

    The same problem coming back after a fix is a regression, which is more
    severe than a plain duplicate: the original issue is reopened and flagged
    instead of being left closed with a silent duplicate pointing at it.
    """

    issue_number: int
    issue_url: str
    closed_at: str = ""
    assignees: tuple[str, ...] = ()


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
    return TriageResult(
        issue_type=issue_type,
        fields=fields,
        assignee=assignee,
        comment_markdown=comment,
        blame_suggestion=blame_suggestion,
        suspected_owner=suspected_owner,
        follow_up_questions=follow_up_questions,
        duplicate_of=duplicate_of,
    )


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
) -> TriageApplySummary:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)
    if result.duplicate_of == issue_number:
        raise ValueError(f"duplicate_of must reference a different issue, got #{result.duplicate_of}")
    regression = detect_duplicate_regression(
        repo=repo,
        duplicate_of=result.duplicate_of,
        github=github,
    )
    regression_note = render_regression_note(regression)
    fingerprint = triage_result_fingerprint(result)
    decision_key = triage_decision_key(result)
    existing_comments = github.list_issue_comments(repo=repo, issue_number=issue_number)
    existing_field_values = issue_fields.get_issue_field_values(
        repo=repo,
        issue_number=issue_number,
    )
    duplicate = _has_applied_triage_decision(
        comments=existing_comments,
        fingerprint=fingerprint,
        decision_key=decision_key,
        result=result,
        config=config,
        existing_field_values=existing_field_values,
    )
    github.set_issue_type(repo=repo, issue_number=issue_number, issue_type=result.issue_type)
    issue_fields.add_issue_field_values(
        repo=repo,
        issue_number=issue_number,
        values=triage_field_values_for_write(result, config=config),
        config=config,
    )
    if not duplicate:
        if lark is not None and result.fields["Triage status"] == "Needs info":
            _send_lark_follow_up(
                repo=repo,
                issue_number=issue_number,
                result=result,
                github=github,
                lark=lark,
                branch_note=branch_note,
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
            )
        github.add_issue_comment(
            repo=repo,
            issue_number=issue_number,
            body=append_triage_metadata(
                _with_runner_attribution(
                    render_triage_comment(result, branch_note=branch_note, regression_note=regression_note),
                    run_stats=run_stats,
                ),
                {
                    "version": 1,
                    "issue": issue_number,
                    "result_fingerprint": fingerprint,
                    "decision_key": decision_key,
                    "duplicate_of": result.duplicate_of,
                    "regression_of": regression.issue_number if regression is not None else 0,
                    "verdict": result.fields.get("Triage verdict", ""),
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
    if result.duplicate_of:
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
        # A clear duplicate is the only verdict that auto-closes: the work
        # already lives on the original issue. Everything else -- including
        # 预期行为 -- goes to an owner, who decides whether to close it.
        github.close_issue_as_duplicate(
            repo=repo,
            issue_number=issue_number,
            duplicate_of=result.duplicate_of,
        )
        closed_as_duplicate = True
    else:
        github.add_assignee(repo=repo, issue_number=issue_number, assignee=result.assignee)
    return TriageApplySummary(
        issue_type_written=True,
        fields_written=True,
        assignee_written=not result.duplicate_of,
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
    """Flag the original issue as a regression, then reopen it.

    Reopening is last on purpose: ``detect_duplicate_regression`` keys off the
    original still being closed, so a failure before the reopen leaves the
    regression detectable on a re-run instead of silently losing the flag.
    """

    github.add_issue_comment(
        repo=repo,
        issue_number=regression.issue_number,
        body=render_regression_flag_comment(
            regression=regression,
            duplicate_issue_number=duplicate_issue_number,
            duplicate_issue_url=duplicate_issue_url,
        ),
    )
    if lark is not None:
        send_intake_topic_message(
            repo=repo,
            issue_number=regression.issue_number,
            github=github,
            lark=lark,
            text=render_regression_lark_message(
                regression=regression,
                duplicate_issue_number=duplicate_issue_number,
                duplicate_issue_url=duplicate_issue_url,
                assignee_open_ids=config.lark.user_open_ids or {},
            ),
        )
    github.reopen_issue(repo=repo, issue_number=regression.issue_number)
    # A reopened issue must re-enter the triage pipeline: leave its field on the
    # stale "Running" from the old run and dispatch_due_triage defers it forever
    # (fived #4324). Reset to Pending so the watcher re-dispatches.
    issue_fields.add_issue_field_values(
        repo=repo,
        issue_number=regression.issue_number,
        values={"Triage status": "Pending"},
        config=config,
    )


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


def detect_duplicate_regression(
    *,
    repo: str,
    duplicate_of: int,
    github: GitHubCliIssuesClient,
) -> DuplicateRegression | None:
    """Return the original issue when a duplicate points at a fixed-and-closed issue.

    Only a close that means "this was fixed" counts: ``not_planned`` and
    ``duplicate`` closes were never fixes, so a new report of them is not a
    regression. A missing ``state_reason`` (older closes) is treated as
    completed, which is how GitHub renders it.
    """

    if not duplicate_of:
        return None
    original = github.get_issue(repo=repo, issue_number=duplicate_of)
    if original.state != "closed":
        return None
    if original.state_reason in ("not_planned", "duplicate"):
        return None
    return DuplicateRegression(
        issue_number=original.number,
        issue_url=original.url,
        closed_at=original.closed_at,
        assignees=original.assignees,
    )


def render_regression_note(regression: DuplicateRegression | None) -> str:
    if regression is None:
        return ""
    return (
        f"⚠️ 疑似回归（regression）：原 issue #{regression.issue_number} 曾以「已修复」关闭，"
        "同一问题再次出现，已自动重新打开原 issue 并标记回归。"
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
            "## ⚠️ 疑似回归（regression）",
            "",
            f"本 issue 此前已作为「已修复」关闭{closed_note}，"
            f"但新上报的 [#{duplicate_issue_number}]({duplicate_issue_url}) 被分诊判定为同一问题。",
            "同一问题在修复后再次出现，说明修复可能失效、被回退，或存在未覆盖的触发路径，"
            "因此本 issue 已被自动重新打开。",
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
            f"{head}⚠️ 疑似回归（regression）：本 issue "
            f"[#{regression.issue_number}]({regression.issue_url}) 曾以「已修复」关闭，"
            f"但新上报的 [#{duplicate_issue_number}]({duplicate_issue_url}) 是同一问题。",
            "已自动重新打开本 issue，请确认修复是否失效或被回退。",
        ]
    )


def render_triage_comment(
    result: TriageResult,
    *,
    branch_note: str = "",
    regression_note: str = "",
) -> str:
    body = result.comment_markdown.rstrip()
    if result.blame_suggestion or result.suspected_owner:
        if "Blame" not in body and "归因" not in body:
            parts = []
            if result.suspected_owner:
                parts.append(f"疑似引入人（Owner）：{result.suspected_owner}")
            if result.blame_suggestion:
                parts.append(f"归因线索：{result.blame_suggestion}")
            body = f"{body}\n\n" + "\n".join(parts)
    prefix_lines = []
    if regression_note:
        prefix_lines.append(f"> {regression_note}")
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
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("triage metadata must be a JSON object")
    return data


def _has_applied_triage_decision(
    *,
    comments: tuple[GitHubIssueComment, ...],
    fingerprint: str,
    decision_key: str,
    result: TriageResult,
    config: ProjectConfig,
    existing_field_values: dict[str, str],
) -> bool:
    has_prior_triage = False
    for comment in comments:
        metadata = parse_triage_metadata(comment.body)
        if metadata is not None and metadata.get("result_fingerprint") == fingerprint:
            return True
        if metadata is not None and metadata.get("decision_key") == decision_key:
            return True
        if metadata is not None and _triage_comment_matches_decision(comment.body, result):
            return True
        if metadata is not None:
            has_prior_triage = True
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
    chat_id = _metadata_str(metadata, "chat_id")
    message_id = _metadata_str(metadata, "message_id")
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
) -> str:
    lines = [f"分诊完成，GitHub issue [#{issue_number}]({issue_url})"]
    if branch_note:
        lines.append(branch_note)
    if regression_note:
        lines.append(regression_note)
    if result.duplicate_of:
        duplicate_url = f"{issue_url.rsplit('/', 1)[0]}/{result.duplicate_of}"
        lines.append(f"结论：重复，已关闭。重复于 [#{result.duplicate_of}]({duplicate_url})")
    else:
        assignee = result.assignee
        if assignee and assignee_open_id:
            assignee = f'<at user_id="{assignee_open_id}">{result.assignee}</at>'
        for label, value in (
            ("结论", result.fields.get("Triage verdict", "")),
            ("状态", result.fields.get("Triage status", "")),
            ("优先级", result.fields.get("Priority", "")),
            ("负责人", assignee),
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
) -> str:
    prefix = f'<at user_id="{reporter_open_id}"></at> ' if reporter_open_id else ""
    lines = [f"{prefix}需要补充信息，GitHub issue [#{issue_number}]({issue_url})"]
    if branch_note:
        lines.append(branch_note)
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
