"""Backfill Lark messages into the intake workflow."""

from __future__ import annotations

from dataclasses import dataclass

from bugpatrol.config import ProjectConfig
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeOutcome, IntakeWorkflow
from bugpatrol.lark import LarkMessage, LarkOpenApiMessengerClient


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
) -> BackfillResult:
    messages = lark.list_chat_messages(chat_id=config.lark.chat_id, limit=limit)
    outcomes: list[IntakeOutcome] = []
    skipped = 0
    for message in reversed(messages):
        if should_skip_message(message, bot_open_id=config.lark.bot_open_id):
            skipped += 1
            continue
        record = intake_record_from_lark_message(message)
        if dry_run:
            skipped += 1
            continue
        outcomes.append(workflow.process(record))
    return BackfillResult(
        scanned=len(messages),
        processed=len(outcomes),
        skipped=skipped,
        outcomes=tuple(outcomes),
    )


def should_skip_message(message: LarkMessage, *, bot_open_id: str) -> bool:
    if not message.message_id:
        return True
    if message.msg_type != "text":
        return True
    if message.sender_open_id == bot_open_id:
        return True
    text = message.text.strip()
    if not text:
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
    )
