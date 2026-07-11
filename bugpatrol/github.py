"""GitHub issue client backed by the GitHub CLI."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bugpatrol.clients import (
    GitHubIssue,
    GitHubIssueComment,
    GitHubPullRequest,
    OpenPullRequest,
    ReviewComment,
    ReviewThread,
)
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

    def list_issues(self, *, repo: str, state: str = "open") -> tuple[GitHubIssue, ...]:
        result = self._run(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                state,
                "--limit",
                str(self._search_limit),
                "--json",
                "number,url,title,body",
            ]
        )
        return tuple(
            GitHubIssue(
                number=int(item["number"]),
                url=str(item["url"]),
                title=str(item["title"]),
                body=str(item.get("body") or ""),
            )
            for item in json.loads(result.stdout)
        )

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

    def list_issue_comments(self, *, repo: str, issue_number: int) -> tuple[GitHubIssueComment, ...]:
        result = self._run(
            [
                "api",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                f"/repos/{repo}/issues/{issue_number}/comments",
            ]
        )
        data = json.loads(result.stdout)
        return tuple(
            GitHubIssueComment(id=str(item["id"]), body=str(item.get("body") or ""))
            for item in data
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
            state=str(data.get("state") or ""),
            state_reason=str(data.get("state_reason") or ""),
            assignees=tuple(
                str(item["login"])
                for item in data.get("assignees") or ()
                if isinstance(item, dict) and item.get("login")
            ),
        )

    def list_issue_timeline(self, *, repo: str, issue_number: int) -> tuple[dict, ...]:
        result = self._run(
            [
                "api",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{repo}/issues/{issue_number}/timeline?per_page=100",
            ]
        )
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            raise GitHubCliError(f"unexpected timeline response for {repo}#{issue_number}")
        return tuple(item for item in data if isinstance(item, dict))

    def get_pull_request(self, *, repo: str, pr: str) -> GitHubPullRequest:
        result = self._run(
            [
                "pr",
                "view",
                pr,
                "--repo",
                repo,
                "--json",
                "number,url,title,body,closingIssuesReferences,baseRefName",
            ]
        )
        data = json.loads(result.stdout)
        closing = data.get("closingIssuesReferences")
        closing_numbers: list[int] = []
        if isinstance(closing, list):
            for item in closing:
                if isinstance(item, dict) and isinstance(item.get("number"), int):
                    closing_numbers.append(int(item["number"]))
        return GitHubPullRequest(
            number=int(data["number"]),
            url=str(data["url"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            closing_issue_numbers=tuple(closing_numbers),
            timeline_issue_numbers=self._timeline_issue_numbers_for_pr(repo=repo, pr_number=int(data["number"])),
            base_ref=str(data.get("baseRefName") or ""),
        )

    def list_merged_pull_requests(
        self, *, repo: str, limit: int = 30
    ) -> tuple[GitHubPullRequest, ...]:
        """Recent merged PRs with their closing-issue references.

        Bounded by `limit`. Timeline-linked issues are left empty here (one API
        call per PR would be wasteful for reconcile); reconcile resolves the
        associated issue from closingIssuesReferences plus title/body refs.
        """
        result = self._run(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "merged",
                "--limit",
                str(limit),
                "--json",
                "number,url,title,body,closingIssuesReferences",
            ]
        )
        pulls: list[GitHubPullRequest] = []
        for data in json.loads(result.stdout):
            closing = data.get("closingIssuesReferences")
            closing_numbers: list[int] = []
            if isinstance(closing, list):
                for item in closing:
                    if isinstance(item, dict) and isinstance(item.get("number"), int):
                        closing_numbers.append(int(item["number"]))
            pulls.append(
                GitHubPullRequest(
                    number=int(data["number"]),
                    url=str(data.get("url") or ""),
                    title=str(data.get("title") or ""),
                    body=str(data.get("body") or ""),
                    closing_issue_numbers=tuple(closing_numbers),
                )
            )
        return tuple(pulls)

    def _timeline_issue_numbers_for_pr(self, *, repo: str, pr_number: int) -> tuple[int, ...]:
        try:
            result = self._run(
                [
                    "api",
                    "-H",
                    f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"/repos/{repo}/issues/{pr_number}/timeline",
                ]
            )
        except GitHubCliError:
            return ()
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            return ()
        from bugpatrol.fix_notify import issue_numbers_from_timeline_events

        return issue_numbers_from_timeline_events(data, exclude=(pr_number,))

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

    def close_issue_as_duplicate(self, *, repo: str, issue_number: int, duplicate_of: int) -> None:
        owner, name = repo.split("/", 1)
        result = self._run(
            [
                "api",
                "graphql",
                "-f",
                "query=query($owner: String!, $name: String!, $issue: Int!, $duplicate: Int!) {"
                " repository(owner: $owner, name: $name) {"
                " issue(number: $issue) { id }"
                " duplicate: issue(number: $duplicate) { id } } }",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"issue={issue_number}",
                "-F",
                f"duplicate={duplicate_of}",
            ]
        )
        data = json.loads(result.stdout)
        repository = data.get("data", {}).get("repository") or {}
        issue_id = (repository.get("issue") or {}).get("id")
        duplicate_id = (repository.get("duplicate") or {}).get("id")
        if not isinstance(issue_id, str) or not isinstance(duplicate_id, str):
            raise GitHubCliError(f"cannot resolve issue node ids for {repo}#{issue_number} / #{duplicate_of}")
        self._run(
            [
                "api",
                "graphql",
                "-f",
                "query=mutation($issue: ID!, $duplicate: ID!) {"
                " closeIssue(input: {issueId: $issue, stateReason: DUPLICATE, duplicateIssueId: $duplicate}) {"
                " issue { number state } } }",
                "-f",
                f"issue={issue_id}",
                "-f",
                f"duplicate={duplicate_id}",
            ]
        )

    def remote_branch_tip_sha(self, *, repo: str, branch: str) -> str:
        """Best-effort current tip SHA of a remote branch; "" if unavailable.

        Recorded at intake so a branch later merged and deleted can still be
        classified. Never raises: a missing branch just yields "".
        """
        try:
            result = self._run(
                [
                    "api",
                    f"repos/{repo}/git/ref/heads/{branch}",
                    "--jq",
                    ".object.sha",
                ]
            )
        except GitHubCliError:
            return ""
        return result.stdout.strip()

    def find_open_pull_request_by_head(self, *, repo: str, head: str) -> str:
        """URL of an open PR whose head branch is `head`, or "" if none.

        Used for fix idempotency: a fix run must not open a second PR for an
        issue that already has an open one.
        """
        result = self._run(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                head,
                "--state",
                "open",
                "--json",
                "url",
            ]
        )
        data = json.loads(result.stdout)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return str(data[0].get("url") or "")
        return ""

    def get_open_pull_request_by_head(self, *, repo: str, head: str) -> OpenPullRequest | None:
        """Open PR whose head branch is `head`, or None.

        Like `find_open_pull_request_by_head` but returns the number, target
        branch, and mergeability too, which revise needs to read/resolve the
        PR's review threads and to detect/resolve a conflict with the target
        branch. Kept separate so the existing idempotency short-circuit's return
        type is unchanged.
        """
        result = self._run(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                head,
                "--state",
                "open",
                "--json",
                "number,url,baseRefName,mergeable",
            ]
        )
        data = json.loads(result.stdout)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            number = data[0].get("number")
            url = data[0].get("url")
            if isinstance(number, int) and isinstance(url, str):
                return OpenPullRequest(
                    number=number,
                    url=url,
                    base_ref=str(data[0].get("baseRefName") or ""),
                    mergeable=str(data[0].get("mergeable") or ""),
                )
        return None

    def list_unresolved_review_threads(self, *, repo: str, pr_number: int) -> tuple[ReviewThread, ...]:
        """Unresolved review threads on a PR (the revise work queue).

        State lives entirely in GitHub: any runner can read the same queue and,
        after addressing a thread, resolve it so it is not reprocessed.
        """
        owner, name = repo.split("/", 1)
        result = self._run(
            [
                "api",
                "graphql",
                "-f",
                "query=query($owner: String!, $name: String!, $pr: Int!) {"
                " repository(owner: $owner, name: $name) {"
                " pullRequest(number: $pr) {"
                " reviewThreads(first: 100) { nodes {"
                " id isResolved"
                " comments(first: 50) { nodes {"
                " body path line author { login } } } } } } } }",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"pr={pr_number}",
            ]
        )
        data = json.loads(result.stdout)
        pull = (
            (data.get("data", {}).get("repository") or {}).get("pullRequest") or {}
        )
        nodes = (pull.get("reviewThreads") or {}).get("nodes") or []
        threads: list[ReviewThread] = []
        for node in nodes:
            if not isinstance(node, dict) or node.get("isResolved"):
                continue
            thread_id = node.get("id")
            if not isinstance(thread_id, str):
                continue
            comments: list[ReviewComment] = []
            for raw in (node.get("comments") or {}).get("nodes") or []:
                if not isinstance(raw, dict):
                    continue
                author = ((raw.get("author") or {}).get("login")) or ""
                line = raw.get("line")
                comments.append(
                    ReviewComment(
                        author=str(author),
                        body=str(raw.get("body") or ""),
                        path=str(raw.get("path") or ""),
                        line=line if isinstance(line, int) else None,
                    )
                )
            threads.append(ReviewThread(id=thread_id, comments=tuple(comments)))
        return tuple(threads)

    def resolve_review_thread(self, *, thread_id: str) -> None:
        """Mark a review thread resolved once revise has addressed it."""
        self._run(
            [
                "api",
                "graphql",
                "-f",
                "query=mutation($thread: ID!) {"
                " resolveReviewThread(input: {threadId: $thread}) {"
                " thread { id isResolved } } }",
                "-f",
                f"thread={thread_id}",
            ]
        )

    def add_pull_request_comment(self, *, repo: str, pr: str, body: str) -> None:
        """Append a comment to a PR conversation (revise progress reply)."""
        self._run(
            [
                "pr",
                "comment",
                pr,
                "--repo",
                repo,
                "--body-file",
                "-",
            ],
            stdin=body,
        )

    def create_pull_request(
        self,
        *,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        """Open a PR from `head` into `base`; return the PR URL."""
        result = self._run(
            [
                "pr",
                "create",
                "--repo",
                repo,
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            stdin=body,
        )
        return result.stdout.strip().splitlines()[-1].strip()

    def add_pull_request_reviewer(self, *, repo: str, pr: str, reviewer: str) -> None:
        """Request a review from `reviewer`; a non-collaborator reviewer or a
        self-review is not fatal to the fix run, so failures are surfaced by the
        caller rather than raised here."""
        self._run(
            [
                "pr",
                "edit",
                pr,
                "--repo",
                repo,
                "--add-reviewer",
                reviewer,
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
