"""Lark event-stream watcher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence

from bugpatrol.backfill import (
    BackfillEvent,
    BackfillResult,
    intake_record_from_lark_message,
    should_skip_message,
    skip_reason,
)
from bugpatrol.config import ProjectConfig
from bugpatrol.event_log import JsonlEventLog
from bugpatrol.intake_workflow import IntakeOutcome, IntakeWorkflow
from bugpatrol.ledger import JsonMessageLedger, MessageLedger
from bugpatrol.lark import LarkOpenApiMessengerClient
from bugpatrol.lark_events import lark_message_from_event
from bugpatrol.resources import (
    LocalResourceStore,
    ResourceDescriber,
    ResourcePolicy,
    ResourceStore,
    materialize_lark_attachments,
)
from bugpatrol.triage_queue import CommandTriageDispatcher, TriageRequestQueue
from bugpatrol.watcher import (
    TriageDispatcher,
    TriageStatusReader,
    dispatch_due_triage,
    enqueue_triage_outcomes,
    write_backfill_events,
)


def iter_json_event_lines(lines: Iterable[str]) -> Iterable[dict[str, object]]:
    for line in lines:
        text = line.strip()
        if not text:
            continue
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("event line must be a JSON object")
        yield data


def run_lark_event_watcher(
    *,
    config: ProjectConfig,
    event_payloads: Iterable[dict[str, object]],
    lark: LarkOpenApiMessengerClient,
    workflow: IntakeWorkflow,
    dry_run: bool = False,
    resource_dir: Path | None = None,
    resource_store: ResourceStore | None = None,
    resource_describer: ResourceDescriber | None = None,
    resource_policy: ResourcePolicy | None = None,
    event_log_path: Path | None = None,
    event_log: JsonlEventLog | None = None,
    processed_ledger_path: Path | None = None,
    processed_ledger: MessageLedger | None = None,
    triage_queue_path: Path | None = None,
    triage_queue: TriageRequestQueue | None = None,
    triage_quiet_seconds: float = 60,
    triage_dispatch_command: str | Sequence[str] | None = None,
    triage_dispatcher: TriageDispatcher | None = None,
    triage_status_reader: TriageStatusReader | None = None,
) -> BackfillResult:
    if resource_dir is not None and resource_store is not None:
        raise ValueError("resource_dir and resource_store are mutually exclusive")
    if event_log is not None and event_log_path is not None:
        raise ValueError("event_log and event_log_path are mutually exclusive")
    if processed_ledger is not None and processed_ledger_path is not None:
        raise ValueError("processed_ledger and processed_ledger_path are mutually exclusive")
    if triage_queue is not None and triage_queue_path is not None:
        raise ValueError("triage_queue and triage_queue_path are mutually exclusive")
    if triage_dispatcher is not None and triage_dispatch_command is not None:
        raise ValueError("triage_dispatcher and triage_dispatch_command are mutually exclusive")

    logger = event_log or (JsonlEventLog(event_log_path) if event_log_path is not None else None)
    ledger = processed_ledger or (
        JsonMessageLedger.load(processed_ledger_path) if processed_ledger_path is not None else None
    )
    queue = triage_queue or (TriageRequestQueue.load(triage_queue_path) if triage_queue_path is not None else None)
    dispatcher = triage_dispatcher or (
        CommandTriageDispatcher(triage_dispatch_command) if triage_dispatch_command is not None else None
    )

    scanned = 0
    skipped = 0
    outcomes: list[IntakeOutcome] = []
    events: list[BackfillEvent] = []

    for payload in event_payloads:
        scanned += 1
        message = lark_message_from_event(payload, default_chat_id=config.lark.chat_id)
        if message.chat_id != config.lark.chat_id:
            skipped += 1
            events.append(BackfillEvent(message_id=message.message_id, action="skipped", reason="wrong_chat"))
            continue
        if should_skip_message(message, bot_open_id=config.lark.bot_open_id):
            skipped += 1
            events.append(
                BackfillEvent(
                    message_id=message.message_id,
                    action="skipped",
                    reason=skip_reason(message, bot_open_id=config.lark.bot_open_id),
                )
            )
            continue
        if ledger is not None and ledger.is_processed(message.message_id):
            skipped += 1
            events.append(BackfillEvent(message_id=message.message_id, action="skipped", reason="processed_ledger"))
            continue
        record = intake_record_from_lark_message(message)
        if dry_run:
            skipped += 1
            events.append(BackfillEvent(message_id=message.message_id, action="skipped", reason="dry_run"))
            continue
        store = resource_store
        if store is None and resource_dir is not None:
            store = LocalResourceStore(resource_dir)
        if store is not None:
            record = materialize_lark_attachments(
                record=record,
                lark=lark,
                store=store,
                describer=resource_describer,
                policy=resource_policy,
            )
        outcome = workflow.process(record)
        outcomes.append(outcome)
        events.append(
            BackfillEvent(
                message_id=message.message_id,
                action="processed",
                reason=outcome.action,
                issue_number=outcome.issue.number,
            )
        )
        if ledger is not None:
            ledger.mark_processed(message.message_id)

    result = BackfillResult(
        scanned=scanned,
        processed=len(outcomes),
        skipped=skipped,
        outcomes=tuple(outcomes),
        events=tuple(events),
    )
    if logger is not None:
        logger.write(
            {
                "event": "event_watch_scan",
                "scanned": result.scanned,
                "processed": result.processed,
                "skipped": result.skipped,
            }
        )
        write_backfill_events(logger=logger, iteration=1, events=result.events)
    if queue is not None:
        enqueue_triage_outcomes(
            outcomes=result.outcomes,
            queue=queue,
            triage_quiet_seconds=triage_quiet_seconds,
        )
        if dispatcher is not None:
            dispatch_due_triage(
                queue=queue,
                dispatcher=dispatcher,
                triage_quiet_seconds=triage_quiet_seconds,
                status_reader=triage_status_reader,
            )
    return result
