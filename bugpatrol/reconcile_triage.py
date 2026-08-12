"""Find intook-but-untriaged issues and optionally run triage on them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bugpatrol.clients import LarkMessengerClient
from bugpatrol.config import ProjectConfig
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import parse_intake_metadata
from bugpatrol.triage_result import TRIAGE_META_START
from bugpatrol.triage_runner import (
    execute_triage_run,
    prepare_triage_run,
    report_workflow_failure,
    triage_run_in_flight,
)

# Statuses execute_triage_run returns instead of applying a result; each is
# recoverable by re-running with fresh context, and the last attempt forces a
# result rather than returning one of these.
RETRYABLE_TRIAGE_STATUSES = frozenset({"no_output", "invalid_output", "stale_context"})
MAX_TRIAGE_ATTEMPTS = 3


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
    issue_fields: GitHubIssueFieldsClient | None = None,
    repo_path: Path | None = None,
    output_dir: Path = Path(".bugpatrol/triage-run"),
    execute: bool = False,
    run_triage: Callable[[int], str] | None = None,
    lark: LarkMessengerClient | None = None,
) -> ReconcileTriageResult:
    candidates, events_tuple, scanned = find_untriaged_issues(config=config, github=github)
    events = list(events_tuple)

    if execute and run_triage is None:
        if repo_path is None or issue_fields is None:
            raise ValueError("repo_path and issue_fields are required when execute=True")

        def _run_triage(issue_number: int) -> str:
            # Same retry contract as the run-triage CLI: a run that ends without
            # applying a result must be retried with fresh context, and the last
            # attempt must produce a result instead of returning silently.
            for attempt in range(1, MAX_TRIAGE_ATTEMPTS + 1):
                final_attempt = attempt == MAX_TRIAGE_ATTEMPTS
                plan = prepare_triage_run(
                    config=config,
                    issue_number=issue_number,
                    repo_path=repo_path,
                    output_dir=output_dir / str(issue_number),
                    github=github,
                )
                status = execute_triage_run(
                    config=config,
                    issue_number=issue_number,
                    plan=plan,
                    github=github,
                    issue_fields=issue_fields,
                    lark=lark,
                    accept_stale_context=final_attempt,
                    final_attempt=final_attempt,
                )
                if final_attempt or status not in RETRYABLE_TRIAGE_STATUSES:
                    return status
            raise AssertionError("unreachable: the final attempt always returns")

        run_triage = _run_triage

    for candidate in candidates:
        if not execute:
            events.append(
                ReconcileTriageEvent(issue_number=candidate.issue_number, action="candidate", reason="dry_run")
            )
            continue
        assert run_triage is not None
        try:
            status = run_triage(candidate.issue_number)
        except Exception as error:  # noqa: BLE001 - one bad issue must not abort the batch; failure is surfaced in events
            if issue_fields is not None:
                _report_reconcile_triage_failure(
                    config=config,
                    github=github,
                    issue_fields=issue_fields,
                    lark=lark,
                    issue_number=candidate.issue_number,
                    error=error,
                )
            events.append(
                ReconcileTriageEvent(issue_number=candidate.issue_number, action="failed", reason=str(error))
            )
            continue
        # Report the run's own status: "triaged/executed" for every non-raising
        # run once hid runs that ended without applying anything.
        events.append(
            ReconcileTriageEvent(issue_number=candidate.issue_number, action="triaged", reason=status)
        )

    return ReconcileTriageResult(
        scanned=scanned,
        candidates=candidates,
        events=tuple(events),
    )


def _report_reconcile_triage_failure(
    *,
    config: ProjectConfig,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None,
    issue_number: int,
    error: Exception,
) -> None:
    """Make reconcile-discovered triage failures visible on the issue.

    Reconcile is the recovery path for jobs that died before writing a terminal
    triage result. If the retry also fails, an internal JSON event is not enough:
    the issue must carry a durable Failed status/comment so humans and later
    automation can see that it is no longer just "running".
    """
    try:
        report_workflow_failure(
            config=config,
            issue_number=issue_number,
            job="triage",
            github=github,
            issue_fields=issue_fields,
            lark=lark,
            detail=f"reconcile retry failed: {error}",
        )
    except Exception as report_error:  # noqa: BLE001 - last-resort issue comment is better than silence
        try:
            github.add_issue_comment(
                repo=config.github_repo,
                issue_number=issue_number,
                body=(
                    "## BugPatrol triage failed\n\n"
                    f"Reconcile retried triage but it failed before producing a result: {error}\n\n"
                    f"Failure reporting also failed: {report_error}"
                ),
            )
        except Exception:
            # The batch event still records the original failure. Do not let a
            # broken last-resort comment abort reconciliation for other issues.
            return
