"""GitHub issue client backed by the GitHub CLI."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from bugpatrol.clients import GitHubIssue


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


class GitHubCliError(RuntimeError):
    pass


class GitHubCliIssuesClient:
    def __init__(self, *, gh: str = "gh", search_limit: int = 200) -> None:
        self._gh = gh
        self._search_limit = search_limit

    def find_issue_by_intake_root(self, *, repo: str, chat_id: str, root_id: str) -> GitHubIssue | None:
        result = self._run(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--limit",
                str(self._search_limit),
                "--json",
                "number,url,title,body",
            ]
        )
        for item in json.loads(result.stdout):
            body = str(item.get("body") or "")
            if f'"chat_id":"{chat_id}"' in body and f'"root_id":"{root_id}"' in body:
                return GitHubIssue(
                    number=int(item["number"]),
                    url=str(item["url"]),
                    title=str(item["title"]),
                    body=body,
                )
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
        # Current gh on this host does not expose native Issue Type or Issue
        # Fields flags. Those are handled by the future GitHub field writer.
        del issue_type, fields
        result = self._run(
            ["issue", "create", "--repo", repo, "--title", title, "--body-file", "-"],
            stdin=body,
        )
        url = result.stdout.strip().splitlines()[-1].strip()
        number = _issue_number_from_url(url)
        return GitHubIssue(number=number, url=url, title=title, body=body)

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        self._run(
            ["issue", "comment", str(issue_number), "--repo", repo, "--body-file", "-"],
            stdin=body,
        )

    def close_issue(self, *, repo: str, issue_number: int, reason: str = "not planned") -> None:
        self._run(
            [
                "issue",
                "close",
                str(issue_number),
                "--repo",
                repo,
                "--reason",
                reason,
                "--comment",
                "Closed automatically by bugpatrol live e2e cleanup.",
            ]
        )

    def _run(self, args: Sequence[str], *, stdin: str | None = None) -> CommandResult:
        completed = subprocess.run(
            [self._gh, *args],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitHubCliError(
                f"gh {' '.join(args)} failed with exit {completed.returncode}: {completed.stderr.strip()}"
            )
        return CommandResult(stdout=completed.stdout, stderr=completed.stderr)


def _issue_number_from_url(url: str) -> int:
    match = re.search(r"/issues/(\d+)$", url)
    if not match:
        raise GitHubCliError(f"cannot parse issue number from URL: {url}")
    return int(match.group(1))

