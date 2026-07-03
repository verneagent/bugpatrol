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


@dataclass(frozen=True)
class FixEventCandidate:
    event: str
    issue_number: int | None = None
    pr: str = ""
    commit: str = ""


@dataclass(frozen=True)
class FixReconcileResult:
    attempted: int
    sent: int
    duplicate_skipped: int
    skipped: int
    summaries: tuple[FixNotificationSummary, ...]
    errors: tuple[str, ...]


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
    numbers.update(pr.timeline_issue_numbers)
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


def issue_numbers_from_timeline_events(events: tuple[dict[str, Any], ...] | list[object], *, exclude: tuple[int, ...] = ()) -> tuple[int, ...]:
    excluded = set(exclude)
    numbers: set[int] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        numbers.update(_issue_numbers_from_timeline_event(event))
    return tuple(sorted(number for number in numbers if number not in excluded))


def reconcile_fix_notifications(
    *,
    repo: str,
    candidates: tuple[FixEventCandidate, ...],
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None = None,
    dry_run: bool = True,
) -> FixReconcileResult:
    summaries: list[FixNotificationSummary] = []
    errors: list[str] = []
    skipped = 0
    for candidate in candidates:
        issue_number = candidate.issue_number
        if issue_number is None:
            if candidate.event not in ("pr_opened", "pr_merged") or not candidate.pr:
                skipped += 1
                errors.append(f"{candidate.event}: missing issue_number")
                continue
            try:
                issue_number = resolve_single_issue_from_pr(
                    github.get_pull_request(repo=repo, pr=candidate.pr)
                )
            except ValueError as error:
                skipped += 1
                errors.append(str(error))
                continue
        summary = apply_fix_notification(
            repo=repo,
            issue_number=issue_number,
            event=candidate.event,
            pr=candidate.pr,
            commit=candidate.commit,
            github=github,
            lark=lark,
            dry_run=dry_run,
        )
        summaries.append(summary)
    return FixReconcileResult(
        attempted=len(candidates),
        sent=sum(1 for summary in summaries if summary.lark_sent),
        duplicate_skipped=sum(1 for summary in summaries if summary.duplicate_skipped),
        skipped=skipped,
        summaries=tuple(summaries),
        errors=tuple(errors),
    )


def fix_event_candidates_from_json(data: object) -> tuple[FixEventCandidate, ...]:
    if not isinstance(data, list):
        raise ValueError("fix reconcile input must be a JSON array")
    candidates: list[FixEventCandidate] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("fix reconcile item must be an object")
        event = item.get("event")
        if not isinstance(event, str):
            raise ValueError("fix reconcile item missing event")
        raw_issue = item.get("issue") if "issue" in item else item.get("issue_number")
        issue_number = int(raw_issue) if isinstance(raw_issue, (int, str)) and str(raw_issue).strip() else None
        candidates.append(
            FixEventCandidate(
                event=event,
                issue_number=issue_number,
                pr=_optional_str(item.get("pr")),
                commit=_optional_str(item.get("commit")),
            )
        )
    return tuple(candidates)


def _issue_numbers_from_timeline_event(event: dict[str, Any]) -> set[int]:
    numbers: set[int] = set()
    source = event.get("source")
    if isinstance(source, dict) and source.get("type") == "issue":
        issue = source.get("issue")
        if isinstance(issue, dict) and isinstance(issue.get("number"), int):
            numbers.add(int(issue["number"]))
    subject = event.get("subject")
    if isinstance(subject, dict) and subject.get("type") == "issue" and isinstance(subject.get("number"), int):
        numbers.add(int(subject["number"]))
    issue = event.get("issue")
    if isinstance(issue, dict) and isinstance(issue.get("number"), int):
        numbers.add(int(issue["number"]))
    return numbers


def _optional_str(value: object) -> str:
    return value if isinstance(value, str) else ""


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
