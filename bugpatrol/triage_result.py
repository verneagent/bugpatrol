"""Validate and apply triage agent results."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from bugpatrol.config import ProjectConfig
from bugpatrol.fields import NATIVE_ISSUE_TYPES, default_field_specs, validate_field_value
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient


@dataclass(frozen=True)
class TriageResult:
    issue_type: str
    fields: dict[str, str]
    assignee: str
    comment_markdown: str


@dataclass(frozen=True)
class TriageApplySummary:
    issue_type_written: bool
    fields_written: bool
    assignee_written: bool
    comment_added: bool
    duplicate_comment_skipped: bool
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
    assignee = _required_str(data, "assignee").lstrip("@")
    comment = _required_str(data, "comment_markdown")
    return TriageResult(
        issue_type=issue_type,
        fields=fields,
        assignee=assignee,
        comment_markdown=comment,
    )


def apply_triage_result(
    *,
    repo: str,
    issue_number: int,
    config: ProjectConfig,
    result: TriageResult,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
) -> TriageApplySummary:
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


def triage_result_fingerprint(result: TriageResult) -> str:
    payload = {
        "issue_type": result.issue_type,
        "fields": result.fields,
        "assignee": result.assignee,
        "comment_markdown": result.comment_markdown,
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


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing string field: {key}")
    return value
