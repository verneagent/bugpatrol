"""GitHub Issue Fields support."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from bugpatrol.config import ProjectConfig
from bugpatrol.fields import FieldSpec
from bugpatrol.gh_transient import is_transient_gh_error

GITHUB_API_VERSION = "2026-03-10"


class GitHubIssueFieldsError(RuntimeError):
    pass


@dataclass(frozen=True)
class IssueField:
    id: int
    name: str
    data_type: str
    options: tuple[str, ...] = ()


class GitHubIssueFieldsClient:
    def __init__(
        self,
        *,
        gh: str = "gh",
        transient_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._gh = gh
        self._transient_retries = transient_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleep = sleep

    def list_org_fields(self, *, org: str) -> dict[str, IssueField]:
        data = json.loads(
            self._run_api(
                ["-H", f"X-GitHub-Api-Version: {GITHUB_API_VERSION}", f"/orgs/{org}/issue-fields"]
            )
        )
        fields: dict[str, IssueField] = {}
        for item in data:
            options = tuple(str(option["name"]) for option in item.get("options", ()))
            field = IssueField(
                id=int(item["id"]),
                name=str(item["name"]),
                data_type=str(item["data_type"]),
                options=options,
            )
            fields[field.name] = field
        return fields

    def add_issue_field_values(
        self,
        *,
        repo: str,
        issue_number: int,
        values: dict[str, str | int | float | list[str]],
        config: ProjectConfig,
    ) -> None:
        owner, name = split_repo(repo)
        fields = self.list_org_fields(org=owner)
        payload = {
            "issue_field_values": build_issue_field_values_payload(
                config=config,
                live_fields=fields,
                logical_values=values,
            )
        }
        self._run_api(
            [
                "-X",
                "POST",
                "-H",
                f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                f"/repos/{owner}/{name}/issues/{issue_number}/issue-field-values",
                "--input",
                "-",
            ],
            stdin=json.dumps(payload, ensure_ascii=False),
        )

    def get_issue_field_values(self, *, repo: str, issue_number: int) -> dict[str, str]:
        owner, name = split_repo(repo)
        data = json.loads(
            self._run_api(
                [
                    "-H",
                    f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                    f"/repos/{owner}/{name}/issues/{issue_number}/issue-field-values",
                ]
            )
        )
        values: dict[str, str] = {}
        for item in data:
            field_name = str(item["issue_field_name"])
            option = item.get("single_select_option")
            if isinstance(option, dict) and isinstance(option.get("name"), str):
                values[field_name] = str(option["name"])
            elif item.get("value") is not None:
                values[field_name] = str(item["value"])
        return values

    def get_issue_state(self, *, repo: str, issue_number: int) -> str:
        owner, name = split_repo(repo)
        data = json.loads(
            self._run_api(
                [
                    "-H",
                    f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
                    f"/repos/{owner}/{name}/issues/{issue_number}",
                ]
            )
        )
        return str(data.get("state", ""))

    def _run_api(self, args: Sequence[str], *, stdin: str | None = None) -> str:
        # Bounded retry on transient gateway/transport blips (e.g. a
        # `net/http: TLS handshake timeout` mid-poll) so a single flaky call
        # doesn't crash the whole watcher. GET reads are idempotent; the one
        # mutation (add_issue_field_values POST) is a set-to-value upsert, so
        # re-applying the same values is a no-op — safe to retry.
        for attempt in range(1, self._transient_retries + 1):
            completed = subprocess.run(
                [self._gh, "api", *args],
                input=stdin,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode == 0:
                return completed.stdout
            stderr = completed.stderr.strip()
            if "Not Found" in stderr and "/issue-fields" in " ".join(args):
                raise GitHubIssueFieldsError(
                    "GitHub Issue Fields are only available for organization-owned repositories "
                    "with Issue Fields enabled."
                )
            if attempt < self._transient_retries and is_transient_gh_error(stderr):
                self._sleep(self._retry_backoff_seconds * attempt)
                continue
            raise GitHubIssueFieldsError(
                f"gh api {' '.join(args)} failed with exit {completed.returncode}: {stderr}"
            )
        raise AssertionError("unreachable")  # loop always returns or raises


def build_issue_field_values_payload(
    *,
    config: ProjectConfig,
    live_fields: dict[str, IssueField],
    logical_values: dict[str, str | int | float | list[str]],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for logical_name, value in logical_values.items():
        github_name = config.issue_field_names.get(logical_name)
        if not github_name:
            raise GitHubIssueFieldsError(f"missing field mapping for {logical_name!r}")
        field = live_fields.get(github_name)
        if field is None:
            raise GitHubIssueFieldsError(f"GitHub issue field not found: {github_name!r}")
        if field.data_type == "single_select":
            if not isinstance(value, str):
                raise GitHubIssueFieldsError(f"{github_name!r} expects a string option")
            if field.options and value not in field.options:
                raise GitHubIssueFieldsError(
                    f"{github_name!r} option {value!r} is not one of {sorted(field.options)}"
                )
        elif field.data_type == "multi_select":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise GitHubIssueFieldsError(f"{github_name!r} expects a string array")
            missing = sorted(set(value) - set(field.options))
            if field.options and missing:
                raise GitHubIssueFieldsError(f"{github_name!r} unknown options: {missing}")
        elif field.data_type == "text":
            if not isinstance(value, str):
                raise GitHubIssueFieldsError(f"{github_name!r} expects text")
        elif field.data_type == "number":
            if not isinstance(value, (int, float)):
                raise GitHubIssueFieldsError(f"{github_name!r} expects a number")
        elif field.data_type == "date":
            if not isinstance(value, str):
                raise GitHubIssueFieldsError(f"{github_name!r} expects an ISO date string")
        else:
            raise GitHubIssueFieldsError(f"unsupported issue field data type: {field.data_type}")
        payload.append({"field_id": field.id, "value": value})
    return payload


def find_field_option_drift(
    specs: dict[str, FieldSpec],
    live_fields: dict[str, IssueField],
) -> list[str]:
    """Return drift lines where a declared spec option is absent from the live
    org field. An empty list means the canonical specs are a subset of GitHub.

    Only select-type fields that actually exist in the org are checked; a field
    the org has not provisioned is out of scope here. This exists to fail a
    triage run fast (before the agent runs) instead of crashing mid-apply when
    someone adds an option to ``fields.py`` without syncing the org field.
    """
    drift: list[str] = []
    for name, spec in specs.items():
        field = live_fields.get(name)
        if field is None:
            continue
        if field.data_type not in ("single_select", "multi_select"):
            continue
        missing = [value for value in spec.values if value not in field.options]
        if missing:
            drift.append(
                f"{name!r} is missing option(s) {missing} — add them to the org "
                f"issue field (live options: {sorted(field.options)})"
            )
    return drift


def split_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise GitHubIssueFieldsError(f"invalid repo: {repo!r}")
    return parts[0], parts[1]
