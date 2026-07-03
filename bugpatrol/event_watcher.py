"""Lark event-stream watcher."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

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
    ResourceRedactor,
    ResourceStore,
    ResourceTransformer,
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


@dataclass(frozen=True)
class ReconnectPolicy:
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    multiplier: float = 2.0
    max_attempts: int = 0


def iter_json_event_lines(lines: Iterable[str]) -> Iterable[dict[str, object]]:
    for line in lines:
        text = line.strip()
        if not text:
            continue
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("event line must be a JSON object")
        if is_heartbeat_payload(data):
            continue
        yield data


def is_heartbeat_payload(payload: dict[str, object]) -> bool:
    event_type = _payload_type(payload)
    return event_type in {"heartbeat", "ping", "pong"}


def iter_reconnecting_event_payloads(
    *,
    connect: Callable[[], Iterable[str]],
    policy: ReconnectPolicy = ReconnectPolicy(),
    sleep: Callable[[float], None] = time.sleep,
    retriable_exceptions: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError),
) -> Iterable[dict[str, object]]:
    attempt = 0
    delay = max(0.0, policy.initial_delay_seconds)
    max_delay = max(delay, policy.max_delay_seconds)
    while True:
        try:
            yield from iter_json_event_lines(connect())
            return
        except retriable_exceptions:
            attempt += 1
            if policy.max_attempts > 0 and attempt >= policy.max_attempts:
                raise
            sleep(delay)
            delay = min(max_delay, delay * max(1.0, policy.multiplier))


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
    resource_redactor: ResourceRedactor | None = None,
    resource_transformer: ResourceTransformer | None = None,
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
        if should_skip_message(message, bot_open_id=config.lark.bot_open_id, bot_app_id=config.lark.app_id):
            skipped += 1
            events.append(
                BackfillEvent(
                    message_id=message.message_id,
                    action="skipped",
                    reason=skip_reason(message, bot_open_id=config.lark.bot_open_id, bot_app_id=config.lark.app_id),
                )
            )
            continue
        if ledger is not None and ledger.is_processed(message.message_id):
            skipped += 1
            events.append(BackfillEvent(message_id=message.message_id, action="skipped", reason="processed_ledger"))
            continue
        record = intake_record_from_lark_message(
            message,
            sender_names=config.lark.sender_names or {},
            message_url_template=config.lark.message_url_template,
        )
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
                redactor=resource_redactor,
                transformer=resource_transformer,
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


def _payload_type(payload: dict[str, object]) -> str:
    value = payload.get("type")
    if not isinstance(value, str):
        header = payload.get("header")
        if isinstance(header, dict):
            value = header.get("event_type")
    if not isinstance(value, str):
        value = payload.get("event_type")
    return value.strip().lower() if isinstance(value, str) else ""
