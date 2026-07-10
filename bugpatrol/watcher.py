"""Polling Lark watcher."""

from __future__ import annotations

import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from bugpatrol.backfill import (
    BranchTipResolver,
    ScanResult,
    TopicResult,
    process_topic_batch,
    scan_topic_batches,
)
from bugpatrol.config import ProjectConfig
from bugpatrol.event_log import JsonlEventLog
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.ledger import JsonMessageLedger, MessageLedger
from bugpatrol.lease import FileLease
from bugpatrol.lark import LarkOpenApiError, LarkOpenApiMessengerClient
from bugpatrol.resources import (
    LocalResourceStore,
    ResourceDescriber,
    ResourcePolicy,
    ResourceRedactor,
    ResourceStore,
    ResourceTransformer,
)
from bugpatrol.triage_queue import CommandTriageDispatcher, TriageRequest, TriageRequestQueue


# Transient Lark scan failures tolerated before the watcher gives up and
# crashes (surfacing a persistent outage to launchd/operators).
MAX_CONSECUTIVE_SCAN_FAILURES = 10


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
    resource_policy: ResourcePolicy | None = None,
    resource_redactor: ResourceRedactor | None = None,
    resource_transformer: ResourceTransformer | None = None,
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
    parallel_topics: int = 1,
    branch_tip_resolver: BranchTipResolver | None = None,
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

    if parallel_topics < 1:
        raise ValueError("parallel_topics must be >= 1")
    store = resource_store
    if store is None and resource_dir is not None:
        store = LocalResourceStore(resource_dir)

    iterations = 0
    scanned = 0
    processed = 0
    skipped = 0
    queued_triage = 0
    dispatched_triage = 0
    in_flight: dict[str, Future[TopicResult]] = {}
    if lease is not None:
        lease.acquire()
    try:
        with ThreadPoolExecutor(max_workers=parallel_topics) as executor:
            consecutive_scan_failures = 0
            while True:
                iterations += 1
                try:
                    scan = _scan_all_chats(
                        config=config,
                        lark=lark,
                        limit=limit,
                        processed_ledger=ledger,
                        exclude_roots=frozenset(in_flight),
                    )
                except LarkOpenApiError as error:
                    # Transient Lark/network failures (timeouts, expired
                    # tokens) must not kill the watcher; retry next poll.
                    # Persistent failures still crash so launchd/operators see
                    # them instead of an eternally silent loop.
                    consecutive_scan_failures += 1
                    if consecutive_scan_failures >= MAX_CONSECUTIVE_SCAN_FAILURES:
                        raise
                    print(
                        f"watch-lark: scan failed ({consecutive_scan_failures}/"
                        f"{MAX_CONSECUTIVE_SCAN_FAILURES}), retrying next poll: {error}",
                        file=sys.stderr,
                    )
                    if logger is not None:
                        logger.write(
                            {
                                "event": "watch_scan_error",
                                "iteration": iterations,
                                "error": str(error),
                            }
                        )
                    if lease is not None:
                        lease.refresh()
                    if once or (max_iterations is not None and iterations >= max_iterations):
                        raise
                    time.sleep(interval_seconds)
                    continue
                consecutive_scan_failures = 0
                for batch in scan.topics:
                    in_flight[batch.root_key] = executor.submit(
                        process_topic_batch,
                        batch,
                        config=config,
                        lark=lark,
                        workflow=workflow,
                        dry_run=dry_run,
                        store=store,
                        resource_describer=resource_describer,
                        resource_policy=resource_policy,
                        resource_redactor=resource_redactor,
                        resource_transformer=resource_transformer,
                        branch_tip_resolver=branch_tip_resolver,
                    )
                final_iteration = once or (max_iterations is not None and iterations >= max_iterations)
                results = _harvest_topic_results(in_flight, wait_all=final_iteration)
                iteration_events = list(scan.skipped_events)
                iteration_outcomes = []
                for result in results:
                    if ledger is not None:
                        for message_id in result.processed_message_ids:
                            ledger.mark_processed(message_id)
                    iteration_events.extend(result.events)
                    iteration_outcomes.extend(result.outcomes)
                iteration_skipped = sum(1 for event in iteration_events if event.action == "skipped")
                scanned += scan.scanned
                processed += len(iteration_outcomes)
                skipped += iteration_skipped
                if logger is not None:
                    logger.write(
                        {
                            "event": "watch_scan",
                            "iteration": iterations,
                            "scanned": scan.scanned,
                            "processed": len(iteration_outcomes),
                            "skipped": iteration_skipped,
                        }
                    )
                    write_backfill_events(logger=logger, iteration=iterations, events=iteration_events)
                if queue is not None:
                    queued_triage += enqueue_triage_outcomes(
                        outcomes=iteration_outcomes,
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
                if final_iteration:
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


def _scan_all_chats(
    *,
    config: ProjectConfig,
    lark: LarkOpenApiMessengerClient,
    limit: int,
    processed_ledger: MessageLedger | None,
    exclude_roots: frozenset[str],
) -> ScanResult:
    """Scan the main chat plus every branch chat in one pass.

    Topic root keys are globally unique Lark message ids, so batches from
    different chats never collide and the existing `exclude_roots` dedup keeps
    each group isolated. Each batch's messages carry their own chat_id, which
    downstream resolves to the declared branch.
    """
    scanned = 0
    skipped_events: list = []
    topics: list = []
    for chat_id in config.lark.all_chat_ids():
        result = scan_topic_batches(
            config=config,
            lark=lark,
            limit=limit,
            processed_ledger=processed_ledger,
            exclude_roots=exclude_roots,
            chat_id=chat_id,
        )
        scanned += result.scanned
        skipped_events.extend(result.skipped_events)
        topics.extend(result.topics)
    return ScanResult(
        scanned=scanned,
        skipped_events=tuple(skipped_events),
        topics=tuple(topics),
    )


def _harvest_topic_results(
    in_flight: dict[str, "Future[TopicResult]"],
    *,
    wait_all: bool,
) -> list[TopicResult]:
    """Collect finished topic results; optionally block for the stragglers.

    `process_topic_batch` never raises, so `future.result()` is safe.
    """
    if wait_all:
        for future in list(in_flight.values()):
            future.exception()  # block until done
    results: list[TopicResult] = []
    for root_key, future in list(in_flight.items()):
        if not future.done():
            continue
        results.append(future.result())
        del in_flight[root_key]
    return results


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
