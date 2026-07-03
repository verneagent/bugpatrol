"""Polling Lark watcher."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from bugpatrol.backfill import BackfillResult, run_lark_backfill
from bugpatrol.config import ProjectConfig
from bugpatrol.intake_workflow import IntakeWorkflow
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
    triage_queue_path: Path | None = None,
    triage_queue: TriageRequestQueue | None = None,
    triage_quiet_seconds: float = 60,
    triage_dispatch_command: str | Sequence[str] | None = None,
    triage_dispatcher: TriageDispatcher | None = None,
) -> WatchResult:
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

    iterations = 0
    scanned = 0
    processed = 0
    skipped = 0
    queued_triage = 0
    dispatched_triage = 0
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
        )
        iterations += 1
        scanned += result.scanned
        processed += result.processed
        skipped += result.skipped
        if queue is not None:
            for outcome in result.outcomes:
                request = queue.enqueue(
                    issue_number=outcome.issue.number,
                    signal=outcome.triage_signal,
                    quiet_seconds=triage_quiet_seconds,
                )
                if request is not None:
                    queued_triage += 1
            if dispatcher is not None:
                for request in queue.due_requests():
                    dispatcher.dispatch(request)
                    queue.mark_dispatched(request)
                    dispatched_triage += 1
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
