"""Validate and apply triage agent results."""

from __future__ import annotations

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
) -> None:
    github.set_issue_type(repo=repo, issue_number=issue_number, issue_type=result.issue_type)
    issue_fields.add_issue_field_values(
        repo=repo,
        issue_number=issue_number,
        values=result.fields,
        config=config,
    )
    github.add_issue_comment(repo=repo, issue_number=issue_number, body=result.comment_markdown)
    github.add_assignee(repo=repo, issue_number=issue_number, assignee=result.assignee)


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing string field: {key}")
    return value
