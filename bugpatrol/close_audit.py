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
from bugpatrol.triage_result import parse_triage_metadata

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
    notified: bool = False
    kind: str = ""
    lark_sent: bool = False
    reopened: bool = False
    skipped_reason: str = ""


# A close is a "major issue event" the reporter/assignee should hear about on
# Lark. Each close reason maps to one notification kind; the kind is also the
# idempotency marker (dedup key), so a re-run of the audit — or a reopen +
# re-close with the *same* reason — never double-pings.
KIND_MISSING_FIX = "missing_fix_reference"  # closed completed, no fix evidence (dev nag)
KIND_NOT_PLANNED = "closed_not_planned"
KIND_DUPLICATE = "closed_duplicate"


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
    metadata = parse_intake_metadata(issue.body or "")
    if metadata is None:
        return CloseAuditSummary(issue_number=issue_number, audited=False, skipped_reason="not bugpatrol-managed")
    if issue.state != "closed":
        return CloseAuditSummary(issue_number=issue_number, audited=False, skipped_reason="issue is not closed")

    comments = github.list_issue_comments(repo=repo, issue_number=issue_number)
    reason = issue.state_reason

    reopen = False
    if reason == "completed":
        evidence = fix_evidence_for_issue(
            timeline=github.list_issue_timeline(repo=repo, issue_number=issue_number),
            comments=comments,
        )
        if evidence:
            # A merged PR / linked fix commit is the canonical "fixed" signal;
            # fix_notify (reconcile) already announces it to Lark. Announcing it
            # again here would double-ping, so close-audit stays silent.
            return CloseAuditSummary(issue_number=issue_number, audited=True, evidence=evidence)
        kind = KIND_MISSING_FIX
        reopen = config.close_audit.reopen_completed_without_evidence
    elif reason == "not_planned":
        if _triage_announced_expected_behavior(comments):
            # Triage's own 预期行为 close already posted the Lark summary; don't
            # re-announce the same close.
            return CloseAuditSummary(
                issue_number=issue_number,
                audited=True,
                skipped_reason="triage already announced expected behavior",
            )
        kind = KIND_NOT_PLANNED
    elif reason == "duplicate":
        if _triage_announced_duplicate(comments):
            # Triage's own duplicate-close already posted the Lark summary; don't
            # re-announce the same close.
            return CloseAuditSummary(
                issue_number=issue_number, audited=True, skipped_reason="triage already announced duplicate"
            )
        kind = KIND_DUPLICATE
    else:
        return CloseAuditSummary(
            issue_number=issue_number,
            audited=False,
            skipped_reason=f"close reason is {reason or 'unknown'}, nothing to notify",
        )

    if reopen:
        # Enforcement: GitHub has no pre-close gate for issues, so we can't
        # reject a "completed" close that lacks a fix reference -- we reopen it
        # instead. Dedup rides on issue *state*: a workflow re-run after our
        # reopen sees the issue already open and skips at the top guard, while a
        # genuine re-close (still without a fix reference) re-fires and reopens
        # again. So this path intentionally does NOT gate on the persistent
        # marker (that would let one manual re-close slip through).
        if dry_run:
            return CloseAuditSummary(issue_number=issue_number, audited=True, kind=kind)
        # Lark first, then reopen, then the record comment: a Lark failure leaves
        # the issue closed so the next run retries; reopening before commenting
        # means a comment failure still left an explanatory Lark ping.
        lark_sent = _send_close_lark(
            lark=lark, metadata=metadata, issue=issue, config=config, kind=kind, reopened=True
        )
        github.reopen_issue(repo=repo, issue_number=issue_number)
        github.add_issue_comment(
            repo=repo,
            issue_number=issue_number,
            body=_render_close_comment(issue=issue, kind=kind, reopened=True),
        )
        return CloseAuditSummary(
            issue_number=issue_number,
            audited=True,
            kind=kind,
            notified=True,
            nagged=True,
            reopened=True,
            lark_sent=lark_sent,
        )

    if _already_notified(comments, kind):
        return CloseAuditSummary(
            issue_number=issue_number, audited=True, kind=kind, skipped_reason="already notified"
        )
    if dry_run:
        return CloseAuditSummary(issue_number=issue_number, audited=True, kind=kind)

    # Send Lark first, then write the GitHub marker comment (which doubles as the
    # idempotency marker) LAST. If the marker went first, a Lark failure would be
    # permanently suppressed on retry — a silent lost ping, which is worse than
    # the rare duplicate a marker-last order can cause.
    lark_sent = _send_close_lark(
        lark=lark, metadata=metadata, issue=issue, config=config, kind=kind, reopened=False
    )
    github.add_issue_comment(
        repo=repo,
        issue_number=issue_number,
        body=_render_close_comment(issue=issue, kind=kind),
    )
    return CloseAuditSummary(
        issue_number=issue_number,
        audited=True,
        kind=kind,
        notified=True,
        nagged=kind == KIND_MISSING_FIX,
        lark_sent=lark_sent,
    )


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


def _already_notified(comments: tuple[GitHubIssueComment, ...], kind: str) -> bool:
    for comment in comments:
        metadata = parse_close_audit_metadata(comment.body)
        if metadata is not None and metadata.get("kind") == kind:
            return True
    return False


def _triage_announced_duplicate(comments: tuple[GitHubIssueComment, ...]) -> bool:
    for comment in comments:
        metadata = parse_triage_metadata(comment.body)
        if metadata is not None and metadata.get("duplicate_of"):
            return True
    return False


def _triage_announced_expected_behavior(comments: tuple[GitHubIssueComment, ...]) -> bool:
    for comment in comments:
        metadata = parse_triage_metadata(comment.body)
        if metadata is not None and metadata.get("verdict") == "预期行为":
            return True
    return False


def _reason_label(kind: str) -> str:
    return {
        KIND_MISSING_FIX: "已修复（completed）",
        KIND_NOT_PLANNED: "不予处理（not planned）",
        KIND_DUPLICATE: "重复（duplicate）",
    }[kind]


def _closer_note(issue) -> str:
    # The closer is a GitHub login we can't map to a Lark open_id — this is
    # attribution, not a ping. Never render it as `@name` (that fakes a Lark
    # mention); label it as a GitHub handle instead.
    return f"，由 {issue.closed_by}（GitHub）关闭" if issue.closed_by else ""


def _send_close_lark(
    *,
    lark: LarkMessengerClient | None,
    metadata: dict[str, Any],
    issue,
    config: ProjectConfig,
    kind: str,
    reopened: bool,
) -> bool:
    if lark is None:
        return False
    chat_id = str(metadata.get("chat_id") or "")
    message_id = str(metadata.get("message_id") or "")
    if not (chat_id and message_id):
        return False
    lark.reply_to_message(
        chat_id=chat_id,
        message_id=message_id,
        text=_render_close_lark_text(
            issue=issue,
            config=config,
            kind=kind,
            reporter_open_id=str(metadata.get("reporter_open_id") or ""),
            reopened=reopened,
        ),
    )
    return True


def _render_close_comment(*, issue, kind: str, reopened: bool = False) -> str:
    metadata = render_close_audit_metadata_comment(
        {"version": 1, "issue": issue.number, "kind": kind}
    )
    if kind == KIND_MISSING_FIX:
        mentions = " ".join(f"@{login}" for login in issue.assignees)
        prefix = f"{mentions} " if mentions else ""
        reopened_note = "，已自动重新打开" if reopened else ""
        closing_hint = (
            "请补充修复出处后再关闭（任选其一）：\n"
            if reopened
            else "请补充修复出处（任选其一）：\n"
        )
        not_code_hint = (
            "如果不是代码修复，请改用 close as not planned / duplicate（不会被重新打开）。"
            if reopened
            else "如果不是代码修复，请改用 close as not planned / duplicate。"
        )
        body = (
            f"{prefix}此 issue 以 completed（已修复）关闭{_closer_note(issue)}，"
            f"但没有找到关联的修复 commit 或已合并 PR{reopened_note}。\n\n"
            f"{closing_hint}"
            f"- 修复 PR / commit message 里写 `Fixes #{issue.number}`（推荐，GitHub 会自动关联）\n"
            f"- 在本 issue 评论里贴修复 commit SHA 或 PR 链接\n\n"
            f"{not_code_hint}"
        )
    else:
        body = f"此 issue 已关闭：{_reason_label(kind)}{_closer_note(issue)}。已通知 Lark 话题。"
    return f"{body}\n\n{metadata}"


def _render_close_lark_text(
    *, issue, config: ProjectConfig, kind: str, reporter_open_id: str, reopened: bool = False
) -> str:
    mentions: list[str] = []
    if reporter_open_id:
        mentions.append(f'<at user_id="{reporter_open_id}">上报人</at>')
    for login in issue.assignees:
        open_id = (config.lark.user_open_ids or {}).get(login, "")
        if open_id:
            mentions.append(f'<at user_id="{open_id}">{login}</at>')
    prefix = ("".join(f"{mention} " for mention in mentions)) if mentions else ""
    if kind == KIND_MISSING_FIX:
        if reopened:
            return (
                f"{prefix}Issue [#{issue.number}]({issue.url}) 以已修复关闭{_closer_note(issue)}，"
                f"但没有关联修复 commit/PR，已自动重新打开。"
                f"请补充修复出处后再关闭，或改用 not planned / duplicate。"
            )
        return (
            f"{prefix}Issue [#{issue.number}]({issue.url}) 被标记为已修复关闭{_closer_note(issue)}，"
            f"但没有关联修复 commit/PR，请在 issue 里补充修复出处。"
        )
    return (
        f"{prefix}Issue [#{issue.number}]({issue.url}) 已关闭：{_reason_label(kind)}{_closer_note(issue)}。"
    )
