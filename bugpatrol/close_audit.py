"""Audit issue closes: closed-as-completed must reference a fix commit/PR."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from bugpatrol.clients import GitHubIssueComment, LarkMessengerClient
from bugpatrol.config import ProjectConfig
from bugpatrol.fix_notify import parse_fix_metadata
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.intake import parse_intake_metadata

CLOSE_AUDIT_META_START = "<!-- BUGPATROL_CLOSE_AUDIT_META"
CLOSE_AUDIT_META_END = "BUGPATROL_CLOSE_AUDIT_META -->"
CLOSE_AUDIT_META_RE = re.compile(
    rf"{re.escape(CLOSE_AUDIT_META_START)}\s*(.*?)\s*{re.escape(CLOSE_AUDIT_META_END)}",
    re.DOTALL,
)


@dataclass(frozen=True)
class CloseAuditSummary:
    issue_number: int
    audited: bool
    evidence: str = ""
    nagged: bool = False
    lark_sent: bool = False
    skipped_reason: str = ""


def audit_issue_close(
    *,
    repo: str,
    issue_number: int,
    config: ProjectConfig,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None = None,
    dry_run: bool = True,
) -> CloseAuditSummary:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    if parse_intake_metadata(issue.body or "") is None:
        return CloseAuditSummary(issue_number=issue_number, audited=False, skipped_reason="not bugpatrol-managed")
    if issue.state != "closed":
        return CloseAuditSummary(issue_number=issue_number, audited=False, skipped_reason="issue is not closed")
    if issue.state_reason != "completed":
        return CloseAuditSummary(
            issue_number=issue_number,
            audited=False,
            skipped_reason=f"close reason is {issue.state_reason or 'unknown'}, not completed",
        )
    comments = github.list_issue_comments(repo=repo, issue_number=issue_number)
    evidence = fix_evidence_for_issue(
        timeline=github.list_issue_timeline(repo=repo, issue_number=issue_number),
        comments=comments,
    )
    if evidence:
        return CloseAuditSummary(issue_number=issue_number, audited=True, evidence=evidence)
    if _already_nagged(comments):
        return CloseAuditSummary(issue_number=issue_number, audited=True, skipped_reason="already nagged")
    if dry_run:
        return CloseAuditSummary(issue_number=issue_number, audited=True)
    # Send Lark first, then write the GitHub nag comment (which doubles as the
    # _already_nagged idempotency marker) LAST. If the marker went first, a Lark
    # failure would be permanently suppressed on retry — a silent lost ping,
    # which is worse than the rare duplicate a marker-last order can cause.
    lark_sent = False
    if lark is not None:
        metadata = parse_intake_metadata(issue.body or "") or {}
        chat_id = str(metadata.get("chat_id") or "")
        message_id = str(metadata.get("message_id") or "")
        if chat_id and message_id:
            lark.reply_to_message(
                chat_id=chat_id,
                message_id=message_id,
                text=_render_nag_lark_text(issue=issue, config=config),
            )
            lark_sent = True
    github.add_issue_comment(
        repo=repo,
        issue_number=issue_number,
        body=_render_nag_comment(issue_number=issue_number, assignees=issue.assignees),
    )
    return CloseAuditSummary(issue_number=issue_number, audited=True, nagged=True, lark_sent=lark_sent)


def fix_evidence_for_issue(
    *,
    timeline: tuple[dict, ...],
    comments: tuple[GitHubIssueComment, ...],
) -> str:
    for event in timeline:
        kind = event.get("event")
        commit_id = event.get("commit_id")
        if kind in ("closed", "referenced") and isinstance(commit_id, str) and commit_id:
            return f"commit {commit_id}"
        if kind == "cross-referenced":
            source = event.get("source")
            issue = source.get("issue") if isinstance(source, dict) else None
            if isinstance(issue, dict):
                pull_request = issue.get("pull_request")
                if isinstance(pull_request, dict) and pull_request.get("merged_at"):
                    return f"merged PR #{issue.get('number')}"
    for comment in comments:
        metadata = parse_fix_metadata(comment.body)
        if metadata is None:
            continue
        pr = str(metadata.get("pr") or "")
        commit = str(metadata.get("commit") or "")
        if pr:
            return f"fix notification PR #{pr.lstrip('#')}"
        if commit:
            return f"fix notification commit {commit}"
    return ""


def render_close_audit_metadata_comment(metadata: dict[str, Any]) -> str:
    return "\n".join(
        [
            CLOSE_AUDIT_META_START,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2),
            CLOSE_AUDIT_META_END,
        ]
    )


def parse_close_audit_metadata(comment_body: str) -> dict[str, Any] | None:
    match = CLOSE_AUDIT_META_RE.search(comment_body)
    if not match:
        return None
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("close audit metadata must be a JSON object")
    return data


def _already_nagged(comments: tuple[GitHubIssueComment, ...]) -> bool:
    return any(parse_close_audit_metadata(comment.body) is not None for comment in comments)


def _render_nag_comment(*, issue_number: int, assignees: tuple[str, ...]) -> str:
    mentions = " ".join(f"@{login}" for login in assignees)
    prefix = f"{mentions} " if mentions else ""
    body = (
        f"{prefix}此 issue 以 completed（已修复）关闭，但没有找到关联的修复 commit 或已合并 PR。\n\n"
        f"请补充修复出处（任选其一）：\n"
        f"- 在本 issue 评论里贴修复 commit SHA 或 PR 链接\n"
        f"- 修复 PR / commit message 里写 `Fixes #{issue_number}`（推荐，GitHub 会自动关联）\n\n"
        f"如果不是代码修复，请改用 close as not planned / duplicate。"
    )
    metadata = render_close_audit_metadata_comment(
        {"version": 1, "issue": issue_number, "kind": "missing_fix_reference"}
    )
    return f"{body}\n\n{metadata}"


def _render_nag_lark_text(*, issue, config: ProjectConfig) -> str:
    mentions = "".join(
        f'<at user_id="{open_id}">{login}</at> '
        for login in issue.assignees
        for open_id in [(config.lark.user_open_ids or {}).get(login, "")]
        if open_id
    )
    return (
        f"{mentions}Issue [#{issue.number}]({issue.url}) 被标记为已修复关闭，"
        f"但没有关联修复 commit/PR，请在 issue 里补充修复出处。"
    )
