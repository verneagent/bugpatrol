"""Reusable fake clients for unit and local e2e tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from bugpatrol.clients import GitHubIssue, GitHubIssueComment, GitHubPullRequest
from bugpatrol.fields import default_field_specs
from bugpatrol.github_fields import IssueField
from bugpatrol.lark import SentLarkMessage
from bugpatrol.triage_ownership import WriteEvent, WriteHistory


@dataclass
class CreatedIssue:
    issue: GitHubIssue
    repo: str
    issue_type: str
    fields: dict[str, str]
    comments: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    closed_as_duplicate_of: int = 0
    reopened: bool = False
    timeline: list[dict[str, Any]] = field(default_factory=list)
    write_events: list[WriteEvent] = field(default_factory=list)
    history_complete: bool = True

    def record_write(self, name: str, actor_type: str = "Bot") -> None:
        self.write_events.append(WriteEvent(str(len(self.write_events) + 1), name, actor_type))


class FakeGitHubIssuesClient:
    def __init__(self) -> None:
        self.created: list[CreatedIssue] = []
        self._next_number = 1
        self.dispatched: list[tuple[str, int]] = []
        self._close_sequence = 0
        # Optional fix-author lookups: tests seed these to exercise the
        # 修复人 attribution; defaults yield no author (empty login).
        self.pull_requests: dict[str, GitHubPullRequest] = {}
        self.commit_authors: dict[str, str] = {}

    def get_pull_request(self, *, repo: str, pr: str) -> GitHubPullRequest:
        if pr in self.pull_requests:
            return self.pull_requests[pr]
        return GitHubPullRequest(
            number=int(str(pr).lstrip("#") or 0),
            url=f"https://github.test/{repo}/pull/{str(pr).lstrip('#')}",
            title="",
            body="",
        )

    def get_commit_author(self, *, repo: str, sha: str) -> str:
        return self.commit_authors.get(sha, "")

    def find_issue_by_intake_root(self, *, repo: str, chat_id: str, root_id: str) -> GitHubIssue | None:
        needle = f'"chat_id":"{chat_id}","root_id":"{root_id}"'
        for item in self.created:
            if item.repo == repo and needle in item.issue.body:
                return item.issue
        return None

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        issue_type: str,
        fields: dict[str, str],
    ) -> GitHubIssue:
        issue = GitHubIssue(
            number=self._next_number,
            url=f"https://github.test/{repo}/issues/{self._next_number}",
            title=title,
            body=body,
            state="open", created_by_type="Bot", issue_type=issue_type,
        )
        self._next_number += 1
        self.created.append(
            CreatedIssue(issue=issue, repo=repo, issue_type=issue_type, fields=dict(fields))
        )
        for name in fields:
            self.created[-1].record_write(name)
        return issue

    def list_issues(self, *, repo: str, state: str = "open") -> tuple[GitHubIssue, ...]:
        return tuple(item.issue for item in self.created if item.repo == repo)

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                item.comments.append(body)
                item.timeline.append({
                    "event": "commented", "id": len(item.comments), "body": body,
                    "actor": {"login": "bugpatrol[bot]", "type": "Bot"},
                })
                return
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def update_issue_body(self, *, repo: str, issue_number: int, body: str) -> None:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                # GitHubIssue is frozen; the anchor PATCH is a body swap.
                item.issue = replace(item.issue, body=body)
                return
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def update_issue_title(self, *, repo: str, issue_number: int, title: str) -> None:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                item.issue = replace(item.issue, title=title)
                item.record_write("title")
                return
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                return item.issue
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def list_issue_comments(self, *, repo: str, issue_number: int) -> tuple[GitHubIssueComment, ...]:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                return tuple(
                    GitHubIssueComment(
                        id=str(index + 1), body=body,
                        author="bugpatrol[bot]", author_type="Bot",
                    )
                    for index, body in enumerate(item.comments)
                )
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def dispatch_triage_run(self, *, repo: str, issue_number: int) -> None:
        self.dispatched.append((repo, issue_number))

    def list_issue_timeline(self, *, repo: str, issue_number: int) -> tuple[dict[str, Any], ...]:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                return tuple(item.timeline)
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def set_issue_type(self, *, repo: str, issue_number: int, issue_type: str) -> None:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                item.issue_type = issue_type
                item.issue = replace(item.issue, issue_type=issue_type)
                item.record_write("issue_type")
                return
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def close_issue_as_duplicate(self, *, repo: str, issue_number: int, duplicate_of: int) -> None:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                item.closed_as_duplicate_of = duplicate_of
                self._close_sequence += 1
                item.issue = replace(
                    item.issue, state="closed", state_reason="duplicate",
                    closed_at=f"2026-10-22T03:00:{self._close_sequence:02d}Z",
                    closed_by="bugpatrol[bot]",
                    closed_by_type="Bot",
                )
                item.timeline.append({
                    "event": "closed", "created_at": item.issue.closed_at,
                    "actor": {"login": "bugpatrol[bot]", "type": "Bot"},
                })
                return
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def reopen_issue(self, *, repo: str, issue_number: int) -> None:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                item.reopened = True
                item.issue = replace(item.issue, state="open", state_reason="", closed_at="")
                item.timeline.append({"event": "reopened", "actor": {"login": "bugpatrol[bot]", "type": "Bot"}})
                return
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def add_assignee(self, *, repo: str, issue_number: int, assignee: str) -> None:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                item.assignees = list(item.issue.assignees)
                if assignee not in item.assignees:
                    item.assignees.append(assignee)
                item.issue = replace(item.issue, assignees=tuple(item.assignees))
                item.record_write("assignee")
                return
        raise ValueError(f"issue not found: {repo}#{issue_number}")

    def get_issue_write_history(self, *, repo: str, issue_number: int) -> WriteHistory:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                return WriteHistory(tuple(item.write_events), complete=item.history_complete)
        raise ValueError(f"issue not found: {repo}#{issue_number}")


class FakeGitHubIssueFieldsClient:
    def __init__(self, github: FakeGitHubIssuesClient) -> None:
        self.github = github

    def get_issue_field_values(self, *, repo: str, issue_number: int) -> dict[str, str]:
        return dict(self._item(repo, issue_number).fields)

    def add_issue_field_values(self, *, repo: str, issue_number: int, values: dict[str, str], **kwargs: Any) -> None:
        item = self._item(repo, issue_number)
        config = kwargs.get("config")
        for name, value in values.items():
            actual_name = config.issue_field_names.get(name, name) if config else name
            item.fields[actual_name] = value
            item.record_write(actual_name)

    def list_org_fields(self, **kwargs: Any) -> dict[str, IssueField]:
        return {
            name: IssueField(id=index, name=name, data_type="single_select", options=spec.values)
            for index, (name, spec) in enumerate(default_field_specs().items())
        }

    def _item(self, repo: str, number: int) -> CreatedIssue:
        return next(item for item in self.github.created if item.repo == repo and item.issue.number == number)


@dataclass(frozen=True)
class LarkReply:
    chat_id: str
    message_id: str
    text: str


@dataclass(frozen=True)
class LarkChatMessage:
    chat_id: str
    text: str


class FakeLarkMessengerClient:
    def __init__(self) -> None:
        self.replies: list[LarkReply] = []
        self.chat_messages: list[LarkChatMessage] = []

    def reply_to_message(self, *, chat_id: str, message_id: str, text: str) -> None:
        self.replies.append(LarkReply(chat_id=chat_id, message_id=message_id, text=text))

    def send_chat_message(self, *, chat_id: str, text: str) -> SentLarkMessage:
        self.chat_messages.append(LarkChatMessage(chat_id=chat_id, text=text))
        return SentLarkMessage(message_id=f"om_sent_{len(self.chat_messages)}")
