"""Notify Lark about explicit fix progress events with GitHub metadata dedupe."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from bugpatrol.clients import GitHubIssue, GitHubIssueComment, GitHubPullRequest, LarkMessengerClient
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.intake import parse_intake_metadata
from bugpatrol.lark import LarkOpenApiError, is_message_withdrawn_error

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
    lark_sent = True
    try:
        lark.reply_to_message(
            chat_id=notification.chat_id,
            message_id=notification.message_id,
            text=notification.text,
        )
    except LarkOpenApiError as error:
        # The Lark thread's root message was recalled — the notification target
        # is permanently gone, so retrying every reconcile pass is futile and
        # would abort the whole batch. Record it as handled (write the marker)
        # and move on; a genuinely transient error still propagates.
        if not is_message_withdrawn_error(error):
            raise
        print(
            f"fix notification for {repo}#{issue_number} skipped: Lark message withdrawn",
            file=sys.stderr,
        )
        lark_sent = False
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
        lark_sent=lark_sent,
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
    # `reply_to_message` turns markdown `[text](url)` into masked Lark links (a
    # rich-text "post"), so every reference is a short label (`#3983`, a 12-char
    # SHA) that is clickable — never a bare full URL or inert `owner/repo#N`.
    def _link(label: str, url: str) -> str:
        return f"[{label}]({url})"

    def _pr_lines(verb: str) -> list[str]:
        number = _normalize_pr(pr)
        lines = [f"修复 PR 已{verb}：{_link(f'#{number}', f'https://github.com/{repo}/pull/{number}')}"]
        if commit:
            lines.append(f"关联 commit：{_link(commit[:12], f'https://github.com/{repo}/commit/{commit}')}")
        return lines

    if event == "pr_opened":
        lines = _pr_lines("创建")
    elif event == "pr_merged":
        lines = _pr_lines("合并")
    elif event == "commit_linked":
        lines = [f"关联修复 commit：{_link(commit[:12], f'https://github.com/{repo}/commit/{commit}')}"]
    elif event == "issue_fixed":
        lines = ["该问题已标记修复"]
    else:
        raise ValueError(f"unsupported fix event: {event}")
    lines.append(f"Issue {_link(f'#{issue.number}', issue.url)}")
    return "\n".join(lines)


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
        try:
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
        except (ValueError, LarkOpenApiError) as error:
            # Isolate a bad candidate (unresolvable issue, transient Lark/API
            # error) so it can't abort the whole batch; no marker is written, so
            # a transient failure is retried on the next reconcile pass.
            skipped += 1
            errors.append(f"{candidate.event} #{issue_number}: {error}")
            continue
        summaries.append(summary)
    return FixReconcileResult(
        attempted=len(candidates),
        sent=sum(1 for summary in summaries if summary.lark_sent),
        duplicate_skipped=sum(1 for summary in summaries if summary.duplicate_skipped),
        skipped=skipped,
        summaries=tuple(summaries),
        errors=tuple(errors),
    )


def collect_fix_candidates_from_github(
    *,
    repo: str,
    github: GitHubCliIssuesClient,
    pr_limit: int = 30,
    closed_issue_limit: int = 100,
    since_days: int = 0,
) -> tuple[FixEventCandidate, ...]:
    """Gather fix candidates from GitHub instead of a hand-authored JSON file.

    Only issues with concrete fix *evidence* qualify (the evidence gate):
      - recently merged PRs -> `pr_merged` per managed closing/referenced issue,
      - closed-as-`completed` managed issues carrying a linked fix commit on
        their timeline -> `commit_linked`, unless the same issue is already
        covered by a merged-PR notification (that PR is the canonical signal).

    An issue closed with no merged PR and no linked fix commit — closed as
    `not_planned`/duplicate, or manually closed with no work — carries no
    evidence and is skipped, so reconcile never announces a "fix" that never
    happened. `since_days > 0` bounds the backfill to a recent window (by PR
    `merged_at` / issue `closed_at`); `0` disables the window.

    Candidates already covered by BUGPATROL_FIX_META are not filtered here; the
    downstream reconcile treats them as duplicates and skips the re-notify, so
    the dedup stays in one place. Bounded by `pr_limit` / `closed_issue_limit`
    to keep the API-call count (and timeline lookups) finite.
    """
    cutoff = _window_cutoff(since_days)
    candidates: list[FixEventCandidate] = []
    seen: set[tuple[str, int | None, str, str]] = set()
    managed_cache: dict[int, bool] = {}
    pr_covered: set[int] = set()

    def _managed(issue_number: int) -> bool:
        if issue_number not in managed_cache:
            issue = github.get_issue(repo=repo, issue_number=issue_number)
            managed_cache[issue_number] = parse_intake_metadata(issue.body or "") is not None
        return managed_cache[issue_number]

    def _add(candidate: FixEventCandidate) -> None:
        marker = (candidate.event, candidate.issue_number, candidate.pr, candidate.commit)
        if marker in seen:
            return
        seen.add(marker)
        candidates.append(candidate)

    for pull in github.list_merged_pull_requests(repo=repo, limit=pr_limit):
        if not _within_window(pull.merged_at, cutoff):
            continue
        for issue_number in associated_issue_numbers_from_pr(pull):
            if _managed(issue_number):
                pr_covered.add(issue_number)
                _add(
                    FixEventCandidate(
                        event="pr_merged",
                        issue_number=issue_number,
                        pr=str(pull.number),
                        commit=pull.merge_commit_sha,
                    )
                )

    for issue in github.list_issues(repo=repo, state="closed")[:closed_issue_limit]:
        if parse_intake_metadata(issue.body or "") is None:
            continue
        managed_cache[issue.number] = True
        if issue.state_reason.lower() != "completed":
            continue
        if not _within_window(issue.closed_at, cutoff):
            continue
        if issue.number in pr_covered:
            continue
        timeline = github.list_issue_timeline(repo=repo, issue_number=issue.number)
        for commit in linked_commits_from_timeline(timeline):
            _add(
                FixEventCandidate(
                    event="commit_linked",
                    issue_number=issue.number,
                    commit=commit,
                )
            )

    return tuple(candidates)


def _window_cutoff(since_days: int) -> datetime | None:
    """Earliest allowed timestamp for a bounded backfill (None = no window)."""
    if since_days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=since_days)


def _within_window(timestamp: str, cutoff: datetime | None) -> bool:
    """True when `timestamp` is at or after `cutoff` (None cutoff = unbounded).

    With an active window, a missing or unparseable timestamp returns False: the
    event can't be placed in time, so a bounded backfill deliberately skips it
    rather than risk re-announcing ancient history.
    """
    if cutoff is None:
        return True
    parsed = _parse_github_timestamp(timestamp)
    if parsed is None:
        return False
    return parsed >= cutoff


def _parse_github_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def linked_commits_from_timeline(
    events: tuple[dict[str, Any], ...] | list[object],
) -> tuple[str, ...]:
    """Commit SHAs referenced by fix-related issue timeline events.

    `referenced` and `closed` events carry the `commit_id` of a commit that
    mentions or closes the issue — the signal for a `commit_linked` fix event.
    """
    commits: list[str] = []
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("event") not in ("referenced", "closed"):
            continue
        commit = event.get("commit_id")
        if isinstance(commit, str) and commit and commit not in seen:
            seen.add(commit)
            commits.append(commit)
    return tuple(commits)


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
