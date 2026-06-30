"""Notify Lark about explicit fix progress events with GitHub metadata dedupe."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from bugpatrol.clients import GitHubIssue, GitHubIssueComment, GitHubPullRequest, LarkMessengerClient
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.intake import parse_intake_metadata

FIX_META_START = "<!-- BUGPATROL_FIX_META"
FIX_META_END = "BUGPATROL_FIX_META -->"
FIX_META_RE = re.compile(
    rf"{re.escape(FIX_META_START)}\s*(.*?)\s*{re.escape(FIX_META_END)}",
    re.DOTALL,
)
FIX_EVENTS = ("pr_opened", "pr_merged", "commit_linked", "issue_fixed")
ISSUE_REF_RE = re.compile(r"(?<![\w/-])(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#([1-9][0-9]*)\b")


@dataclass(frozen=True)
class FixNotification:
    key: str
    event: str
    text: str
    chat_id: str
    message_id: str
    duplicate: bool


@dataclass(frozen=True)
class FixNotificationSummary:
    key: str
    event: str
    dry_run: bool
    duplicate_skipped: bool
    lark_sent: bool
    metadata_written: bool


def build_fix_notification(
    *,
    repo: str,
    issue: GitHubIssue,
    comments: tuple[GitHubIssueComment, ...],
    event: str,
    pr: str = "",
    commit: str = "",
) -> FixNotification:
    if event not in FIX_EVENTS:
        raise ValueError(f"unsupported fix event: {event}")
    key = fix_notification_key(repo=repo, issue_number=issue.number, event=event, pr=pr, commit=commit)
    metadata = parse_intake_metadata(issue.body or "")
    if metadata is None:
        raise ValueError("issue has no BugPatrol Lark intake metadata")
    chat_id = _metadata_str(metadata, "chat_id")
    message_id = _metadata_str(metadata, "message_id")
    if not chat_id or not message_id:
        raise ValueError("issue Lark metadata is missing chat_id or message_id")
    return FixNotification(
        key=key,
        event=event,
        text=render_fix_notification_text(
            issue=issue,
            event=event,
            repo=repo,
            pr=pr,
            commit=commit,
        ),
        chat_id=chat_id,
        message_id=message_id,
        duplicate=key in notified_fix_keys(comments),
    )


def apply_fix_notification(
    *,
    repo: str,
    issue_number: int,
    event: str,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None = None,
    pr: str = "",
    commit: str = "",
    dry_run: bool = True,
) -> FixNotificationSummary:
    issue = github.get_issue(repo=repo, issue_number=issue_number)
    comments = github.list_issue_comments(repo=repo, issue_number=issue_number)
    notification = build_fix_notification(
        repo=repo,
        issue=issue,
        comments=comments,
        event=event,
        pr=pr,
        commit=commit,
    )
    if dry_run or notification.duplicate:
        return FixNotificationSummary(
            key=notification.key,
            event=event,
            dry_run=dry_run,
            duplicate_skipped=notification.duplicate,
            lark_sent=False,
            metadata_written=False,
        )
    if lark is None:
        raise ValueError("lark client is required when dry_run is false")
    lark.reply_to_message(
        chat_id=notification.chat_id,
        message_id=notification.message_id,
        text=notification.text,
    )
    github.add_issue_comment(
        repo=repo,
        issue_number=issue_number,
        body=render_fix_metadata_comment(
            {
                "version": 1,
                "issue": issue_number,
                "key": notification.key,
                "event": event,
                "pr": pr,
                "commit": commit,
            }
        ),
    )
    return FixNotificationSummary(
        key=notification.key,
        event=event,
        dry_run=False,
        duplicate_skipped=False,
        lark_sent=True,
        metadata_written=True,
    )


def fix_notification_key(
    *,
    repo: str,
    issue_number: int,
    event: str,
    pr: str = "",
    commit: str = "",
) -> str:
    if event in ("pr_opened", "pr_merged"):
        if not pr:
            raise ValueError(f"{event} requires pr")
        return f"{event}:{repo}#{_normalize_pr(pr)}"
    if event == "commit_linked":
        if not commit:
            raise ValueError("commit_linked requires commit")
        return f"commit:{repo}@{commit}"
    if event == "issue_fixed":
        return f"issue_fixed:{repo}#{issue_number}"
    raise ValueError(f"unsupported fix event: {event}")


def render_fix_notification_text(
    *,
    issue: GitHubIssue,
    event: str,
    repo: str,
    pr: str = "",
    commit: str = "",
) -> str:
    if event == "pr_opened":
        return f"修复 PR 已创建：{repo}#{_normalize_pr(pr)}\nIssue #{issue.number}: {issue.url}"
    if event == "pr_merged":
        return f"修复 PR 已合并：{repo}#{_normalize_pr(pr)}\nIssue #{issue.number}: {issue.url}"
    if event == "commit_linked":
        return f"关联修复 commit：{repo}@{commit}\nIssue #{issue.number}: {issue.url}"
    if event == "issue_fixed":
        return f"该问题已标记修复：Issue #{issue.number}: {issue.url}"
    raise ValueError(f"unsupported fix event: {event}")


def render_fix_metadata_comment(metadata: dict[str, Any]) -> str:
    return "\n".join(
        [
            FIX_META_START,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2),
            FIX_META_END,
        ]
    )


def parse_fix_metadata(comment_body: str) -> dict[str, Any] | None:
    match = FIX_META_RE.search(comment_body)
    if not match:
        return None
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("fix metadata must be a JSON object")
    return data


def notified_fix_keys(comments: tuple[GitHubIssueComment, ...]) -> set[str]:
    keys: set[str] = set()
    for comment in comments:
        metadata = parse_fix_metadata(comment.body)
        if metadata is not None and isinstance(metadata.get("key"), str):
            keys.add(str(metadata["key"]))
    return keys


def associated_issue_numbers_from_pr(pr: GitHubPullRequest) -> tuple[int, ...]:
    numbers = set(pr.closing_issue_numbers)
    for text in (pr.title, pr.body):
        numbers.update(int(match.group(1)) for match in ISSUE_REF_RE.finditer(text or ""))
    return tuple(sorted(numbers))


def resolve_single_issue_from_pr(pr: GitHubPullRequest) -> int:
    numbers = associated_issue_numbers_from_pr(pr)
    if len(numbers) != 1:
        raise ValueError(
            f"PR #{pr.number} must reference exactly one issue for automatic notification; found {list(numbers)}"
        )
    return numbers[0]


def _normalize_pr(pr: str) -> str:
    value = pr.strip()
    if value.startswith("#"):
        value = value[1:]
    if not value:
        raise ValueError("pr must not be empty")
    return value


def _metadata_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""
