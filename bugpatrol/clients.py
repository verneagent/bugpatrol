"""Client protocols used by bugpatrol workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    url: str
    title: str
    body: str


@dataclass(frozen=True)
class GitHubIssueComment:
    id: str
    body: str


class GitHubIssuesClient(Protocol):
    def find_issue_by_intake_root(self, *, repo: str, chat_id: str, root_id: str) -> GitHubIssue | None:
        """Return the issue already linked to a Lark topic root, if one exists."""

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        issue_type: str,
        fields: dict[str, str],
    ) -> GitHubIssue:
        """Create an issue and write initial deterministic metadata."""

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        """Append a comment to an existing issue."""

    def list_issue_comments(self, *, repo: str, issue_number: int) -> tuple[GitHubIssueComment, ...]:
        """Return comments for an issue."""


class LarkMessengerClient(Protocol):
    def reply_to_message(self, *, chat_id: str, message_id: str, text: str) -> None:
        """Reply in Lark so the reporter sees the GitHub backlink."""
