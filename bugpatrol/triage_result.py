"""Validate and apply triage agent results."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from bugpatrol.clients import LarkMessengerClient
from bugpatrol.config import ProjectConfig
from bugpatrol.fields import NATIVE_ISSUE_TYPES, default_field_specs, validate_field_value
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import parse_intake_metadata, require_bugpatrol_managed_issue


@dataclass(frozen=True)
class TriageResult:
    issue_type: str
    fields: dict[str, str]
    assignee: str
    comment_markdown: str
    follow_up_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriageApplySummary:
    issue_type_written: bool
    fields_written: bool
    assignee_written: bool
    comment_added: bool
    duplicate_comment_skipped: bool
    result_fingerprint: str


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
    follow_up_questions = _optional_str_tuple(data, "follow_up_questions")
    if fields["Triage status"] == "Needs info" and not follow_up_questions:
        raise ValueError("Needs info triage requires follow_up_questions")
    assignee = _required_str(data, "assignee").lstrip("@")
    comment = _required_str(data, "comment_markdown")
    return TriageResult(
        issue_type=issue_type,
        fields=fields,
        assignee=assignee,
        comment_markdown=comment,
        follow_up_questions=follow_up_questions,
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
    fingerprint = triage_result_fingerprint(result)
    github.set_issue_type(repo=repo, issue_number=issue_number, issue_type=result.issue_type)
    issue_fields.add_issue_field_values(
        repo=repo,
        issue_number=issue_number,
        values=result.fields,
        config=config,
    )
    duplicate = _has_applied_triage_fingerprint(
        github=github,
        repo=repo,
        issue_number=issue_number,
        fingerprint=fingerprint,
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
        github.add_issue_comment(
            repo=repo,
            issue_number=issue_number,
            body=append_triage_metadata(
                result.comment_markdown,
                {
                    "version": 1,
                    "issue": issue_number,
                    "result_fingerprint": fingerprint,
                },
            ),
        )
    github.add_assignee(repo=repo, issue_number=issue_number, assignee=result.assignee)
    return TriageApplySummary(
        issue_type_written=True,
        fields_written=True,
        assignee_written=True,
        comment_added=not duplicate,
        duplicate_comment_skipped=duplicate,
        result_fingerprint=fingerprint,
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
    for logical_name, proposed in result.fields.items():
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
        comment_markdown=result.comment_markdown,
        result_fingerprint=triage_result_fingerprint(result),
    )


def triage_result_fingerprint(result: TriageResult) -> str:
    payload = {
        "issue_type": result.issue_type,
        "fields": result.fields,
        "assignee": result.assignee,
        "comment_markdown": result.comment_markdown,
        "follow_up_questions": result.follow_up_questions,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def append_triage_metadata(comment_markdown: str, metadata: dict[str, Any]) -> str:
    return (
        f"{comment_markdown.rstrip()}\n\n"
        f"{TRIAGE_META_START}\n"
        f"{json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)}\n"
        f"{TRIAGE_META_END}"
    )


def parse_triage_metadata(comment_body: str) -> dict[str, Any] | None:
    match = TRIAGE_META_RE.search(comment_body)
    if not match:
        return None
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("triage metadata must be a JSON object")
    return data


def _has_applied_triage_fingerprint(
    *,
    github: GitHubCliIssuesClient,
    repo: str,
    issue_number: int,
    fingerprint: str,
) -> bool:
    for comment in github.list_issue_comments(repo=repo, issue_number=issue_number):
        metadata = parse_triage_metadata(comment.body)
        if metadata is not None and metadata.get("result_fingerprint") == fingerprint:
            return True
    return False


def _send_lark_follow_up(
    *,
    repo: str,
    issue_number: int,
    result: TriageResult,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient,
) -> None:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    metadata = parse_intake_metadata(issue.body or "")
    if metadata is None:
        return
    chat_id = _metadata_str(metadata, "chat_id")
    message_id = _metadata_str(metadata, "message_id")
    if not chat_id or not message_id:
        return
    lark.reply_to_message(
        chat_id=chat_id,
        message_id=message_id,
        text=render_needs_info_lark_message(
            issue_number=issue_number,
            issue_url=issue.url,
            questions=result.follow_up_questions,
        ),
    )


def render_needs_info_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    questions: tuple[str, ...],
) -> str:
    lines = [
        f"需要补充信息，GitHub issue #{issue_number}: {issue_url}",
        "",
    ]
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
