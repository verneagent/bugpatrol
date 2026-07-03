"""Backfill Lark messages into the intake workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bugpatrol.config import ProjectConfig
from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.intake_workflow import IntakeOutcome, IntakeWorkflow
from bugpatrol.ledger import MessageLedger
from bugpatrol.lark import LarkMessage, LarkOpenApiMessengerClient
from bugpatrol.resources import LocalResourceStore, ResourceDescriber, ResourceStore, materialize_lark_attachments


@dataclass(frozen=True)
class BackfillResult:
    scanned: int
    processed: int
    skipped: int
    outcomes: tuple[IntakeOutcome, ...]


def run_lark_backfill(
    *,
    config: ProjectConfig,
    lark: LarkOpenApiMessengerClient,
    workflow: IntakeWorkflow,
    limit: int = 20,
    dry_run: bool = False,
    resource_dir: Path | None = None,
    resource_store: ResourceStore | None = None,
    resource_describer: ResourceDescriber | None = None,
    processed_ledger: MessageLedger | None = None,
) -> BackfillResult:
    if resource_dir is not None and resource_store is not None:
        raise ValueError("resource_dir and resource_store are mutually exclusive")
    messages = lark.list_chat_messages(chat_id=config.lark.chat_id, limit=limit)
    outcomes: list[IntakeOutcome] = []
    skipped = 0
    for message in reversed(messages):
        if should_skip_message(message, bot_open_id=config.lark.bot_open_id):
            skipped += 1
            continue
        if processed_ledger is not None and processed_ledger.is_processed(message.message_id):
            skipped += 1
            continue
        record = intake_record_from_lark_message(message)
        if dry_run:
            skipped += 1
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
            )
        outcomes.append(workflow.process(record))
        if processed_ledger is not None:
            processed_ledger.mark_processed(message.message_id)
    return BackfillResult(
        scanned=len(messages),
        processed=len(outcomes),
        skipped=skipped,
        outcomes=tuple(outcomes),
    )


def should_skip_message(message: LarkMessage, *, bot_open_id: str) -> bool:
    if not message.message_id:
        return True
    if message.sender_open_id == bot_open_id:
        return True
    text = message.text.strip()
    attachments = attachments_from_lark_message(message)
    if not text and not attachments:
        return True
    if text.startswith("已创建 GitHub issue #") or text.startswith("已追加到 GitHub issue #"):
        return True
    if "BugPatrol live e2e seed" in text:
        return True
    if "BugPatrol live test" in text:
        return True
    return False


def intake_record_from_lark_message(message: LarkMessage) -> IntakeRecord:
    return IntakeRecord(
        reporter_name=message.sender_open_id or "Lark user",
        reporter_open_id=message.sender_open_id,
        created_at=message.create_time,
        chat_id=message.chat_id,
        root_id=message.root_id,
        message_id=message.message_id,
        original_text=message.text,
        attachments=attachments_from_lark_message(message),
    )


def attachments_from_lark_message(message: LarkMessage) -> tuple[Attachment, ...]:
    content = _parse_content(message.raw_content)
    if message.msg_type == "image":
        key = _content_str(content, "image_key")
        if key:
            return (Attachment(kind="image", url=_lark_resource_url(message, key)),)
    if message.msg_type == "file":
        key = _content_str(content, "file_key")
        name = _content_str(content, "file_name")
        if key:
            return (Attachment(kind="file", url=_lark_resource_url(message, key), description=name),)
    if message.msg_type == "media":
        key = _content_str(content, "file_key") or _content_str(content, "media_key")
        name = _content_str(content, "file_name")
        if key:
            return (Attachment(kind="video", url=_lark_resource_url(message, key), description=name),)
    return ()


def _parse_content(raw_content: str) -> dict[str, object]:
    if not raw_content:
        return {}
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _content_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _lark_resource_url(message: LarkMessage, key: str) -> str:
    return f"lark://message/{message.message_id}/{message.msg_type}/{key}"
