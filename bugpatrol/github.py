"""GitHub issue client backed by the GitHub CLI."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bugpatrol.clients import GitHubIssue
from bugpatrol.github_fields import GITHUB_API_VERSION, GitHubIssueFieldsClient

if TYPE_CHECKING:
    from bugpatrol.config import ProjectConfig


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


class GitHubCliError(RuntimeError):
    pass


class GitHubCliIssuesClient:
    def __init__(
        self,
        *,
        gh: str = "gh",
        search_limit: int = 200,
        issue_fields: GitHubIssueFieldsClient | None = None,
        project_config: "ProjectConfig | None" = None,
    ) -> None:
        self._gh = gh
        self._search_limit = search_limit
        self._issue_fields = issue_fields
        self._project_config = project_config

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
        result = self._run(
            ["issue", "create", "--repo", repo, "--title", title, "--body-file", "-"],
            stdin=body,
        )
        url = result.stdout.strip().splitlines()[-1].strip()
        number = _issue_number_from_url(url)
        self.set_issue_type(repo=repo, issue_number=number, issue_type=issue_type)
        if self._issue_fields is not None:
            if self._project_config is None:
                raise GitHubCliError("project_config is required when issue_fields is configured")
            self._issue_fields.add_issue_field_values(
                repo=repo,
                issue_number=number,
                values=fields,
                config=self._project_config,
            )
        return GitHubIssue(number=number, url=url, title=title, body=body)

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        self._run(
            ["issue", "comment", str(issue_number), "--repo", repo, "--body-file", "-"],
            stdin=body,
        )

    def set_issue_type(self, *, repo: str, issue_number: int, issue_type: str) -> None:
        self._run(
            [
                "api",
                "-X",
                "PATCH",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                f"/repos/{repo}/issues/{issue_number}",
                "-f",
                f"type={issue_type}",
            ]
        )

    def get_issue_type(self, *, repo: str, issue_number: int) -> str:
        result = self._run(
            [
                "api",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                f"/repos/{repo}/issues/{issue_number}",
            ]
        )
        data = json.loads(result.stdout)
        issue_type = data.get("type")
        if not isinstance(issue_type, dict) or not isinstance(issue_type.get("name"), str):
            return ""
        return str(issue_type["name"])

    def get_repository(self, *, repo: str) -> dict[str, object]:
        result = self._run(["api", f"/repos/{repo}"])
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise GitHubCliError(f"unexpected repository response: {data!r}")
        return data

    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        result = self._run(
            [
                "api",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                f"/repos/{repo}/issues/{issue_number}",
            ]
        )
        data = json.loads(result.stdout)
        return GitHubIssue(
            number=int(data["number"]),
            url=str(data["html_url"]),
            title=str(data["title"]),
            body=str(data.get("body") or ""),
        )

    def list_issue_types(self, *, repo: str) -> tuple[str, ...]:
        result = self._run(
            ["api", "-H", f"X-GitHub-Api-Version: {GITHUB_API_VERSION}", f"/repos/{repo}/issue-types"]
        )
        data = json.loads(result.stdout)
        return tuple(str(item["name"]) for item in data)

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

    def add_assignee(self, *, repo: str, issue_number: int, assignee: str) -> None:
        self._run(
            [
                "issue",
                "edit",
                str(issue_number),
                "--repo",
                repo,
                "--add-assignee",
                assignee,
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
