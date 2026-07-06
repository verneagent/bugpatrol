"""Find intook-but-untriaged issues and optionally run triage on them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bugpatrol.config import ProjectConfig
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import parse_intake_metadata
from bugpatrol.triage_result import TRIAGE_META_START
from bugpatrol.triage_runner import execute_triage_run, prepare_triage_run


@dataclass(frozen=True)
class ReconcileTriageCandidate:
    issue_number: int
    url: str
    title: str


@dataclass(frozen=True)
class ReconcileTriageEvent:
    issue_number: int
    action: str
    reason: str


@dataclass(frozen=True)
class ReconcileTriageResult:
    scanned: int
    candidates: tuple[ReconcileTriageCandidate, ...]
    events: tuple[ReconcileTriageEvent, ...]

    @property
    def failed(self) -> tuple[ReconcileTriageEvent, ...]:
        return tuple(event for event in self.events if event.action == "failed")


def find_untriaged_issues(
    *,
    config: ProjectConfig,
    github: GitHubCliIssuesClient,
) -> tuple[tuple[ReconcileTriageCandidate, ...], tuple[ReconcileTriageEvent, ...], int]:
    issues = github.list_issues(repo=config.github_repo, state="open")
    candidates: list[ReconcileTriageCandidate] = []
    events: list[ReconcileTriageEvent] = []
    for issue in issues:
        if parse_intake_metadata(issue.body) is None:
            events.append(
                ReconcileTriageEvent(issue_number=issue.number, action="skipped", reason="not_bugpatrol_managed")
            )
            continue
        comments = github.list_issue_comments(repo=config.github_repo, issue_number=issue.number)
        if any(TRIAGE_META_START in comment.body for comment in comments):
            events.append(
                ReconcileTriageEvent(issue_number=issue.number, action="skipped", reason="already_triaged")
            )
            continue
        candidates.append(
            ReconcileTriageCandidate(issue_number=issue.number, url=issue.url, title=issue.title)
        )
    return tuple(candidates), tuple(events), len(issues)


def reconcile_triage(
    *,
    config: ProjectConfig,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient | None = None,
    repo_path: Path | None = None,
    output_dir: Path = Path(".bugpatrol/triage-run"),
    execute: bool = False,
    run_triage: Callable[[int], None] | None = None,
) -> ReconcileTriageResult:
    candidates, events_tuple, scanned = find_untriaged_issues(config=config, github=github)
    events = list(events_tuple)

    if execute and run_triage is None:
        if repo_path is None or issue_fields is None:
            raise ValueError("repo_path and issue_fields are required when execute=True")

        def _run_triage(issue_number: int) -> None:
            plan = prepare_triage_run(
                config=config,
                issue_number=issue_number,
                repo_path=repo_path,
                output_dir=output_dir / str(issue_number),
                github=github,
            )
            execute_triage_run(
                config=config,
                issue_number=issue_number,
                plan=plan,
                github=github,
                issue_fields=issue_fields,
            )

        run_triage = _run_triage

    for candidate in candidates:
        if not execute:
            events.append(
                ReconcileTriageEvent(issue_number=candidate.issue_number, action="candidate", reason="dry_run")
            )
            continue
        assert run_triage is not None
        try:
            run_triage(candidate.issue_number)
        except Exception as error:  # noqa: BLE001 - one bad issue must not abort the batch; failure is surfaced in events
            events.append(
                ReconcileTriageEvent(issue_number=candidate.issue_number, action="failed", reason=str(error))
            )
            continue
        events.append(
            ReconcileTriageEvent(issue_number=candidate.issue_number, action="triaged", reason="executed")
        )

    return ReconcileTriageResult(
        scanned=scanned,
        candidates=candidates,
        events=tuple(events),
    )
