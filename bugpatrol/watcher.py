"""Polling Lark watcher."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from bugpatrol.backfill import BackfillResult, run_lark_backfill
from bugpatrol.config import ProjectConfig
from bugpatrol.event_log import JsonlEventLog
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.ledger import JsonMessageLedger, MessageLedger
from bugpatrol.lease import FileLease
from bugpatrol.lark import LarkOpenApiMessengerClient
from bugpatrol.resources import ResourceDescriber, ResourceStore
from bugpatrol.triage_queue import CommandTriageDispatcher, TriageRequest, TriageRequestQueue


@dataclass(frozen=True)
class WatchResult:
    iterations: int
    scanned: int
    processed: int
    skipped: int
    queued_triage: int = 0
    dispatched_triage: int = 0


class TriageDispatcher(Protocol):
    def dispatch(self, request: TriageRequest) -> object:
        """Dispatch a due triage request."""


class TriageStatusReader(Protocol):
    def triage_status(self, issue_number: int) -> str:
        """Return the current triage status for an issue."""


class GitHubTriageStatusReader:
    def __init__(self, *, config: ProjectConfig, issue_fields: GitHubIssueFieldsClient) -> None:
        self._config = config
        self._issue_fields = issue_fields

    def triage_status(self, issue_number: int) -> str:
        values = self._issue_fields.get_issue_field_values(
            repo=self._config.github_repo,
            issue_number=issue_number,
        )
        github_name = self._config.issue_field_names["Triage status"]
        return values.get(github_name, "")


def run_polling_watcher(
    *,
    config: ProjectConfig,
    lark: LarkOpenApiMessengerClient,
    workflow: IntakeWorkflow,
    limit: int = 20,
    interval_seconds: float = 30,
    once: bool = False,
    dry_run: bool = False,
    max_iterations: int | None = None,
    resource_dir: Path | None = None,
    resource_store: ResourceStore | None = None,
    resource_describer: ResourceDescriber | None = None,
    event_log_path: Path | None = None,
    event_log: JsonlEventLog | None = None,
    processed_ledger_path: Path | None = None,
    processed_ledger: MessageLedger | None = None,
    lease_file: Path | None = None,
    lease_ttl_seconds: float = 120,
    triage_queue_path: Path | None = None,
    triage_queue: TriageRequestQueue | None = None,
    triage_quiet_seconds: float = 60,
    triage_dispatch_command: str | Sequence[str] | None = None,
    triage_dispatcher: TriageDispatcher | None = None,
    triage_status_reader: TriageStatusReader | None = None,
) -> WatchResult:
    if event_log is not None and event_log_path is not None:
        raise ValueError("event_log and event_log_path are mutually exclusive")
    if processed_ledger is not None and processed_ledger_path is not None:
        raise ValueError("processed_ledger and processed_ledger_path are mutually exclusive")
    if triage_queue is not None and triage_queue_path is not None:
        raise ValueError("triage_queue and triage_queue_path are mutually exclusive")
    if triage_dispatcher is not None and triage_dispatch_command is not None:
        raise ValueError("triage_dispatcher and triage_dispatch_command are mutually exclusive")
    queue = triage_queue
    if queue is None and triage_queue_path is not None:
        queue = TriageRequestQueue.load(triage_queue_path)
    dispatcher = triage_dispatcher
    if dispatcher is None and triage_dispatch_command is not None:
        dispatcher = CommandTriageDispatcher(triage_dispatch_command)
    ledger = processed_ledger
    if ledger is None and processed_ledger_path is not None:
        ledger = JsonMessageLedger.load(processed_ledger_path)
    logger = event_log
    if logger is None and event_log_path is not None:
        logger = JsonlEventLog(event_log_path)
    lease = FileLease(lease_file, ttl_seconds=lease_ttl_seconds) if lease_file is not None else None

    iterations = 0
    scanned = 0
    processed = 0
    skipped = 0
    queued_triage = 0
    dispatched_triage = 0
    if lease is not None:
        lease.acquire()
    try:
        while True:
            result = run_lark_backfill(
                config=config,
                lark=lark,
                workflow=workflow,
                limit=limit,
                dry_run=dry_run,
                resource_dir=resource_dir,
                resource_store=resource_store,
                resource_describer=resource_describer,
                processed_ledger=ledger,
            )
            iterations += 1
            scanned += result.scanned
            processed += result.processed
            skipped += result.skipped
            if logger is not None:
                logger.write(
                    {
                        "event": "watch_scan",
                        "iteration": iterations,
                        "scanned": result.scanned,
                        "processed": result.processed,
                        "skipped": result.skipped,
                    }
                )
                write_backfill_events(logger=logger, iteration=iterations, events=result.events)
            if queue is not None:
                queued_triage += enqueue_triage_outcomes(
                    outcomes=result.outcomes,
                    queue=queue,
                    triage_quiet_seconds=triage_quiet_seconds,
                )
                if dispatcher is not None:
                    dispatched_triage += dispatch_due_triage(
                        queue=queue,
                        dispatcher=dispatcher,
                        triage_quiet_seconds=triage_quiet_seconds,
                        status_reader=triage_status_reader,
                    )
            if lease is not None:
                lease.refresh()
            if once or (max_iterations is not None and iterations >= max_iterations):
                return WatchResult(
                    iterations=iterations,
                    scanned=scanned,
                    processed=processed,
                    skipped=skipped,
                    queued_triage=queued_triage,
                    dispatched_triage=dispatched_triage,
                )
            time.sleep(interval_seconds)
    finally:
        if lease is not None:
            lease.release()


def write_backfill_events(*, logger: JsonlEventLog, iteration: int, events) -> None:  # type: ignore[no-untyped-def]
    for event in events:
        logger.write(
            {
                "event": "lark_message",
                "iteration": iteration,
                "message_id": event.message_id,
                "action": event.action,
                "reason": event.reason,
                "issue_number": event.issue_number,
            }
        )


def enqueue_triage_outcomes(
    *,
    outcomes,
    queue: TriageRequestQueue,
    triage_quiet_seconds: float,
) -> int:  # type: ignore[no-untyped-def]
    queued = 0
    for outcome in outcomes:
        request = queue.enqueue(
            issue_number=outcome.issue.number,
            signal=outcome.triage_signal,
            quiet_seconds=triage_quiet_seconds,
        )
        if request is not None:
            queued += 1
    return queued


def dispatch_due_triage(
    *,
    queue: TriageRequestQueue,
    dispatcher: TriageDispatcher,
    triage_quiet_seconds: float = 60,
    status_reader: TriageStatusReader | None = None,
) -> int:
    dispatched = 0
    for request in queue.due_requests():
        if status_reader is not None and status_reader.triage_status(request.issue_number) == "Running":
            queue.mark_pending_review(request=request, quiet_seconds=triage_quiet_seconds)
            continue
        dispatcher.dispatch(request)
        queue.mark_dispatched(request)
        dispatched += 1
    return dispatched
