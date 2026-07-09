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
from bugpatrol.lark import is_message_withdrawn_error


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
class TriageApplySummary:
    issue_type_written: bool
    fields_written: bool
    assignee_written: bool
    comment_added: bool
    duplicate_comment_skipped: bool
    result_fingerprint: str
    closed_as_duplicate: bool = False


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
) -> TriageApplySummary:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)
    if result.duplicate_of == issue_number:
        raise ValueError(f"duplicate_of must reference a different issue, got #{result.duplicate_of}")
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
            )
        elif lark is not None:
            _send_lark_triage_summary(
                repo=repo,
                issue_number=issue_number,
                result=result,
                github=github,
                lark=lark,
                config=config,
            )
        github.add_issue_comment(
            repo=repo,
            issue_number=issue_number,
            body=append_triage_metadata(
                _with_runner_attribution(render_triage_comment(result)),
                {
                    "version": 1,
                    "issue": issue_number,
                    "result_fingerprint": fingerprint,
                    "decision_key": decision_key,
                    "blame_suggestion": result.blame_suggestion,
                    "suspected_owner": result.suspected_owner,
                },
            ),
        )
    closed_as_duplicate = False
    if result.duplicate_of:
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


def _with_runner_attribution(comment_markdown: str) -> str:
    runner = triage_runner_name()
    if not runner:
        return comment_markdown
    return f"{comment_markdown.rstrip()}\n\n> 分诊执行机：`{runner}`"


def triage_runner_name() -> str:
    """Name of the CI runner executing this triage, for attribution."""
    return os.environ.get("BUGPATROL_RUNNER_NAME") or os.environ.get("RUNNER_NAME", "")


def render_triage_comment(result: TriageResult) -> str:
    body = result.comment_markdown.rstrip()
    if not result.blame_suggestion and not result.suspected_owner:
        return body
    if "Blame" in body or "归因" in body:
        return body
    parts = []
    if result.suspected_owner:
        parts.append(f"疑似引入人（Owner）：{result.suspected_owner}")
    if result.blame_suggestion:
        parts.append(f"归因线索：{result.blame_suggestion}")
    return f"{body}\n\n" + "\n".join(parts)


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
        ),
    )


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
        # The GitHub side of the run is already applied; a withdrawn source
        # message must not fail the run over a best-effort Lark notification.
        if not is_message_withdrawn_error(error):
            raise


def render_triage_summary_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    result: TriageResult,
    assignee_open_id: str = "",
    runner_name: str = "",
) -> str:
    lines = [f"分诊完成，GitHub issue [#{issue_number}]({issue_url})"]
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
    if runner_name:
        lines.append(f"分诊执行机：{runner_name}")
    return "\n".join(lines)


def render_needs_info_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    questions: tuple[str, ...],
    reporter_open_id: str = "",
) -> str:
    prefix = f'<at user_id="{reporter_open_id}"></at> ' if reporter_open_id else ""
    lines = [f"{prefix}需要补充信息，GitHub issue [#{issue_number}]({issue_url})"]
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
