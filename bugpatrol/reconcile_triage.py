"""Find intook-but-untriaged issues and dispatch triage for them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from bugpatrol.config import ProjectConfig
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.intake import parse_intake_metadata
from bugpatrol.triage_result import TRIAGE_META_START
from bugpatrol.triage_runner import triage_run_in_flight


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
        if triage_run_in_flight(comments):
            events.append(
                ReconcileTriageEvent(issue_number=issue.number, action="skipped", reason="triage_in_flight")
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
    execute: bool = False,
    dispatch: Callable[[int], None] | None = None,
) -> ReconcileTriageResult:
    candidates, events_tuple, scanned = find_untriaged_issues(config=config, github=github)
    events = list(events_tuple)

    if execute and dispatch is None:
        # Each candidate is dispatched through its own bugpatrol-triage workflow
        # run, which mints a fresh GitHub App token for the job's lifetime.
        # Running triage in-process here used a single token minted at job start,
        # so a batch of more than ~7 candidates outlived its 1h validity and the
        # tail 401'd while their agents had already paid the cost (run
        # 32005198092). Dispatching also keeps reconcile consistent with the
        # watcher, which fire-and-forgets the same workflow.
        def _dispatch(issue_number: int) -> None:
            github.dispatch_triage_run(repo=config.github_repo, issue_number=issue_number)

        dispatch = _dispatch

    for candidate in candidates:
        if not execute:
            events.append(
                ReconcileTriageEvent(issue_number=candidate.issue_number, action="candidate", reason="dry_run")
            )
            continue
        assert dispatch is not None
        try:
            dispatch(candidate.issue_number)
        except Exception as error:  # noqa: BLE001 - one bad dispatch must not abort the batch
            events.append(
                ReconcileTriageEvent(issue_number=candidate.issue_number, action="failed", reason=str(error))
            )
            continue
        events.append(
            ReconcileTriageEvent(issue_number=candidate.issue_number, action="dispatched", reason="triage_workflow")
        )

    return ReconcileTriageResult(
        scanned=scanned,
        candidates=candidates,
        events=tuple(events),
    )
