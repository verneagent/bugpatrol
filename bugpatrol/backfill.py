"""Backfill Lark messages into the intake workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bugpatrol.config import ProjectConfig
from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.intake_workflow import IntakeOutcome, IntakeWorkflow
from bugpatrol.ledger import MessageLedger
from bugpatrol.lark import LarkMessage, LarkOpenApiMessengerClient
from bugpatrol.resources import (
    LocalResourceStore,
    ResourceDescriber,
    ResourcePolicy,
    ResourceRedactor,
    ResourceStore,
    ResourceTransformer,
    materialize_lark_attachments,
)


@dataclass(frozen=True)
class BackfillEvent:
    message_id: str
    action: str
    reason: str
    issue_number: int | None = None


@dataclass(frozen=True)
class BackfillResult:
    scanned: int
    processed: int
    skipped: int
    outcomes: tuple[IntakeOutcome, ...]
    events: tuple[BackfillEvent, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TopicBatch:
    """Messages of one Lark topic (root), oldest first."""

    root_key: str
    messages: tuple[LarkMessage, ...]


@dataclass(frozen=True)
class ScanResult:
    scanned: int
    skipped_events: tuple[BackfillEvent, ...]
    topics: tuple[TopicBatch, ...]


@dataclass(frozen=True)
class TopicResult:
    root_key: str
    outcomes: tuple[IntakeOutcome, ...]
    events: tuple[BackfillEvent, ...]
    processed_message_ids: tuple[str, ...]
    error: str = ""


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
    resource_policy: ResourcePolicy | None = None,
    resource_redactor: ResourceRedactor | None = None,
    resource_transformer: ResourceTransformer | None = None,
    processed_ledger: MessageLedger | None = None,
) -> BackfillResult:
    if resource_dir is not None and resource_store is not None:
        raise ValueError("resource_dir and resource_store are mutually exclusive")
    messages = lark.list_chat_messages(chat_id=config.lark.chat_id, limit=limit)
    outcomes: list[IntakeOutcome] = []
    events: list[BackfillEvent] = []
    skipped = 0
    since_ms = config.intake.since_ms()
    for message in reversed(messages):
        if should_skip_message(
            message,
            bot_open_id=config.lark.bot_open_id,
            bot_app_id=config.lark.app_id,
            since_ms=since_ms,
        ):
            skipped += 1
            events.append(
                BackfillEvent(
                    message_id=message.message_id,
                    action="skipped",
                    reason=skip_reason(
                        message,
                        bot_open_id=config.lark.bot_open_id,
                        bot_app_id=config.lark.app_id,
                        since_ms=since_ms,
                    ),
                )
            )
            continue
        if processed_ledger is not None and processed_ledger.is_processed(message.message_id):
            skipped += 1
            events.append(
                BackfillEvent(
                    message_id=message.message_id,
                    action="skipped",
                    reason="processed_ledger",
                )
            )
            continue
        store = resource_store
        if store is None and resource_dir is not None:
            store = LocalResourceStore(resource_dir)
        outcome, event = _intake_one_message(
            message,
            config=config,
            lark=lark,
            workflow=workflow,
            dry_run=dry_run,
            store=store,
            resource_describer=resource_describer,
            resource_policy=resource_policy,
            resource_redactor=resource_redactor,
            resource_transformer=resource_transformer,
        )
        events.append(event)
        if outcome is None:
            skipped += 1
            continue
        outcomes.append(outcome)
        if processed_ledger is not None:
            processed_ledger.mark_processed(message.message_id)
    return BackfillResult(
        scanned=len(messages),
        processed=len(outcomes),
        skipped=skipped,
        outcomes=tuple(outcomes),
        events=tuple(events),
    )


def _intake_one_message(
    message: LarkMessage,
    *,
    config: ProjectConfig,
    lark: LarkOpenApiMessengerClient,
    workflow: IntakeWorkflow,
    dry_run: bool = False,
    store: ResourceStore | None = None,
    resource_describer: ResourceDescriber | None = None,
    resource_policy: ResourcePolicy | None = None,
    resource_redactor: ResourceRedactor | None = None,
    resource_transformer: ResourceTransformer | None = None,
) -> tuple[IntakeOutcome | None, BackfillEvent]:
    if (
        config.intake.skip_orphan_replies
        and message.root_id
        and message.root_id != message.message_id
        and not workflow.has_issue_for_root(chat_id=message.chat_id, root_id=message.root_id)
    ):
        # Reply in a topic BugPatrol never intook (e.g. pre-cutover
        # history): appending is impossible and creating a fragment
        # issue from a lone reply would be misleading.
        return None, BackfillEvent(
            message_id=message.message_id,
            action="skipped",
            reason="orphan_reply",
        )
    record = intake_record_from_lark_message(
        message,
        sender_names=config.lark.sender_names or {},
        message_url_template=config.lark.message_url_template,
    )
    if dry_run:
        return None, BackfillEvent(
            message_id=message.message_id,
            action="skipped",
            reason="dry_run",
        )
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
    return outcome, BackfillEvent(
        message_id=message.message_id,
        action="processed",
        reason=outcome.action,
        issue_number=outcome.issue.number,
    )


def scan_topic_batches(
    *,
    config: ProjectConfig,
    lark: LarkOpenApiMessengerClient,
    limit: int = 20,
    processed_ledger: MessageLedger | None = None,
    exclude_roots: frozenset[str] = frozenset(),
) -> ScanResult:
    """Cheap scan phase: filter messages and group the workable ones by topic.

    Topics whose root key is in `exclude_roots` (already in flight on a
    worker) are left untouched so the next scan can pick them up again.
    """
    messages = lark.list_chat_messages(chat_id=config.lark.chat_id, limit=limit)
    skipped_events: list[BackfillEvent] = []
    groups: dict[str, list[LarkMessage]] = {}
    since_ms = config.intake.since_ms()
    for message in reversed(messages):
        reason = skip_reason(
            message,
            bot_open_id=config.lark.bot_open_id,
            bot_app_id=config.lark.app_id,
            since_ms=since_ms,
        )
        if not reason and processed_ledger is not None and processed_ledger.is_processed(message.message_id):
            reason = "processed_ledger"
        if reason:
            skipped_events.append(
                BackfillEvent(message_id=message.message_id, action="skipped", reason=reason)
            )
            continue
        root_key = message.root_id or message.message_id
        if root_key in exclude_roots:
            continue
        groups.setdefault(root_key, []).append(message)
    return ScanResult(
        scanned=len(messages),
        skipped_events=tuple(skipped_events),
        topics=tuple(TopicBatch(root_key=key, messages=tuple(items)) for key, items in groups.items()),
    )


def process_topic_batch(
    batch: TopicBatch,
    *,
    config: ProjectConfig,
    lark: LarkOpenApiMessengerClient,
    workflow: IntakeWorkflow,
    dry_run: bool = False,
    store: ResourceStore | None = None,
    resource_describer: ResourceDescriber | None = None,
    resource_policy: ResourcePolicy | None = None,
    resource_redactor: ResourceRedactor | None = None,
    resource_transformer: ResourceTransformer | None = None,
) -> TopicResult:
    """Process one topic's messages in order. Never raises: an error stops the
    topic and is reported in the result so unprocessed messages retry on the
    next scan."""
    outcomes: list[IntakeOutcome] = []
    events: list[BackfillEvent] = []
    processed_ids: list[str] = []
    error = ""
    for message in batch.messages:
        try:
            outcome, event = _intake_one_message(
                message,
                config=config,
                lark=lark,
                workflow=workflow,
                dry_run=dry_run,
                store=store,
                resource_describer=resource_describer,
                resource_policy=resource_policy,
                resource_redactor=resource_redactor,
                resource_transformer=resource_transformer,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            events.append(
                BackfillEvent(message_id=message.message_id, action="error", reason=error)
            )
            break
        events.append(event)
        if outcome is None:
            continue
        outcomes.append(outcome)
        processed_ids.append(message.message_id)
    return TopicResult(
        root_key=batch.root_key,
        outcomes=tuple(outcomes),
        events=tuple(events),
        processed_message_ids=tuple(processed_ids),
        error=error,
    )


def should_skip_message(
    message: LarkMessage, *, bot_open_id: str, bot_app_id: str = "", since_ms: int = 0
) -> bool:
    return skip_reason(message, bot_open_id=bot_open_id, bot_app_id=bot_app_id, since_ms=since_ms) != ""


def skip_reason(
    message: LarkMessage, *, bot_open_id: str, bot_app_id: str = "", since_ms: int = 0
) -> str:
    if not message.message_id:
        return "missing_message_id"
    if message.deleted:
        return "withdrawn_message"
    if since_ms:
        created_ms = _create_time_ms(message.create_time)
        if created_ms and created_ms < since_ms:
            return "before_intake_since"
    if message.sender_open_id == bot_open_id:
        return "bot_message"
    if bot_app_id and message.sender_type == "app" and message.sender_id == bot_app_id:
        return "bot_message"
    if not _has_reporter_identity(message):
        return "missing_sender"
    if message.msg_type not in {"text", "image", "file", "media", "post"}:
        return "unsupported_msg_type"
    if _is_template_system_message(message):
        return "system_template"
    text = message.text.strip()
    attachments = attachments_from_lark_message(message)
    if not text and not attachments:
        return "empty_message"
    if text.startswith("已创建 GitHub issue #") or text.startswith("已追加到 GitHub issue #"):
        return "bugpatrol_backlink"
    if "BugPatrol live e2e seed" in text:
        return "live_e2e_seed"
    if "BugPatrol live test" in text:
        return "live_test_message"
    return ""


def _create_time_ms(value: str) -> int:
    raw = value.strip()
    if not raw:
        return 0
    try:
        timestamp = int(raw)
    except ValueError:
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    if timestamp <= 0:
        return 0
    return timestamp if timestamp > 10_000_000_000 else timestamp * 1000


def _has_reporter_identity(message: LarkMessage) -> bool:
    if message.sender_open_id:
        return True
    if message.sender_type == "user":
        return True
    if message.sender_type == "app" and message.sender_id_type == "app_id" and message.sender_id:
        return True
    return False


def _is_template_system_message(message: LarkMessage) -> bool:
    content = _parse_content(message.raw_content)
    return "template" in content and "text" not in content


def intake_record_from_lark_message(
    message: LarkMessage,
    *,
    sender_names: dict[str, str] | None = None,
    message_url_template: str = "",
) -> IntakeRecord:
    names = sender_names or {}
    reporter_id = message.sender_open_id or message.sender_id
    reporter_name = _reporter_name(message, sender_names=names)
    topic_url = _message_url_from_template(message_url_template, message=message, target_message_id=message.root_id)
    message_url = _message_url_from_template(
        message_url_template,
        message=message,
        target_message_id=message.message_id,
    )
    return IntakeRecord(
        reporter_name=reporter_name,
        reporter_open_id=reporter_id,
        created_at=message.create_time,
        chat_id=message.chat_id,
        root_id=message.root_id,
        message_id=message.message_id,
        original_text=message.text,
        lark_topic_url=topic_url,
        lark_message_url=message_url,
        attachments=attachments_from_lark_message(message),
    )


def _app_reporter_name(message: LarkMessage, *, display_name: str = "") -> str:
    if message.sender_type == "app" and message.sender_id:
        if display_name:
            return f"{display_name} (Lark app)"
        return "Lark app"
    return ""


def _reporter_name(message: LarkMessage, *, sender_names: dict[str, str]) -> str:
    if message.sender_type == "app":
        return _app_reporter_name(message, display_name=sender_names.get(message.sender_id, ""))
    if message.sender_open_id:
        return sender_names.get(message.sender_open_id) or message.sender_open_id
    return "Lark user"


def _message_url_from_template(template: str, *, message: LarkMessage, target_message_id: str) -> str:
    if not template:
        return ""
    try:
        return template.format(
            chat_id=message.chat_id,
            root_id=message.root_id,
            message_id=target_message_id,
        )
    except KeyError as error:
        raise ValueError(f"unknown lark.message_url_template placeholder: {error}") from error


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
    if message.msg_type == "post":
        return _attachments_from_lark_post(message, content)
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


def _lark_resource_url_with_kind(message: LarkMessage, kind: str, key: str) -> str:
    return f"lark://message/{message.message_id}/{kind}/{key}"


def _attachments_from_lark_post(message: LarkMessage, content: dict[str, object]) -> tuple[Attachment, ...]:
    attachments: list[Attachment] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            file_key = _content_str(value, "file_key")
            image_key = _content_str(value, "image_key")
            name = _content_str(value, "file_name")
            if file_key:
                kind = "video" if value.get("tag") == "media" else "file"
                attachments.append(
                    Attachment(
                        kind=kind,
                        url=_lark_resource_url_with_kind(message, "media" if kind == "video" else "file", file_key),
                        description=name,
                    )
                )
                return
            if image_key:
                attachments.append(Attachment(kind="image", url=_lark_resource_url_with_kind(message, "image", image_key)))
                return
            for child in value.values():
                visit(child)
            return
        if isinstance(value, list):
            for child in value:
                visit(child)

    for key in ("content", "content_v2"):
        visit(content.get(key))
    return tuple(dict.fromkeys(attachments))
