"""Reusable fake clients for unit and local e2e tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from bugpatrol.clients import GitHubIssue


@dataclass
class CreatedIssue:
    issue: GitHubIssue
    repo: str
    issue_type: str
    fields: dict[str, str]
    comments: list[str] = field(default_factory=list)


class FakeGitHubIssuesClient:
    def __init__(self) -> None:
        self.created: list[CreatedIssue] = []
        self._next_number = 1

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
        )
        self._next_number += 1
        self.created.append(
            CreatedIssue(issue=issue, repo=repo, issue_type=issue_type, fields=dict(fields))
        )
        return issue

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        for item in self.created:
            if item.repo == repo and item.issue.number == issue_number:
                item.comments.append(body)
                return
        raise ValueError(f"issue not found: {repo}#{issue_number}")


@dataclass(frozen=True)
class LarkReply:
    chat_id: str
    message_id: str
    text: str


class FakeLarkMessengerClient:
    def __init__(self) -> None:
        self.replies: list[LarkReply] = []

    def reply_to_message(self, *, chat_id: str, message_id: str, text: str) -> None:
        self.replies.append(LarkReply(chat_id=chat_id, message_id=message_id, text=text))
