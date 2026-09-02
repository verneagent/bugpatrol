"""Backfill Lark messages into the intake workflow."""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from bugpatrol.config import ProjectConfig
from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.intake_workflow import IntakeOutcome, IntakeWorkflow
from bugpatrol.ledger import MessageLedger
from bugpatrol.lark import LarkMessage, LarkOpenApiMessengerClient, parse_lark_message
from bugpatrol.resources import (
    LocalResourceStore,
    ResourceDescriber,
    ResourcePolicy,
    ResourceRedactor,
    ResourceStore,
    ResourceTransformer,
    materialize_lark_attachments,
)
from bugpatrol.slash_commands import SlashCommandHandler


# branch name -> best-effort remote tip SHA (or "" when unavailable). Injected
# by the watcher so backfill stays decoupled from GitHub/git access.
BranchTipResolver = Callable[[str], str]

# {chat_id: (fetched_at_monotonic, {open_id: display name})}. The watcher polls
# every few seconds; re-resolving chat members every poll would be a network
# call per chat per cycle. TTL-bounded so new members appear without a watcher
# restart while a chat is never fetched more than ~once a minute.
_CHAT_MEMBERS_TTL_S = 300.0
_chat_members_cache: dict[str, tuple[float, dict[str, str]]] = {}
_chat_members_lock = threading.Lock()


def chat_member_names(
    lark: LarkOpenApiMessengerClient | None,
    chat_id: str,
) -> dict[str, str]:
    """Best-effort {open_id: display name} for a chat, via the bot identity.

    The watcher runs as the Lark app bot, so names come from the bot's own
    tenant token (no user login). Name resolution is best-effort by design:
    failing to resolve must never block intake, so errors warn on stderr and
    return {} and the caller falls back to configured names / bare open_id.
    """
    if lark is None:
        return {}
    now = time.monotonic()
    with _chat_members_lock:
        cached = _chat_members_cache.get(chat_id)
        if cached is not None and now - cached[0] < _CHAT_MEMBERS_TTL_S:
            return cached[1]
    try:
        names = lark.list_chat_members(chat_id=chat_id)
    except Exception as error:  # noqa: BLE001 - resolution is best-effort
        print(f"[bugpatrol] chat member resolution failed for {chat_id}: {error}", file=sys.stderr)
        names = {}
    if not isinstance(names, dict):
        names = {}
    with _chat_members_lock:
        _chat_members_cache[chat_id] = (time.monotonic(), names)
    return names


# Lark delivers a "merged forward" (someone forwards a chat record into the
# group) as a `merge_forward` message whose body is an empty placeholder — the
# real content only comes back from GET /im/v1/messages/{envelope}. The scanners
# expand these into one reportable text message so a forwarded chat intakes
# like any other bug report instead of vanishing as an unsupported msg_type.
MERGE_FORWARD_MSG_TYPE = "merge_forward"
# A merged forward can carry a whole conversation; bound what becomes issue
# text so one forward can't drown a bug body.
_MAX_FORWARDED_ITEMS = 100


def expand_merge_forward(
    lark: LarkOpenApiMessengerClient,
    envelope: LarkMessage,
) -> LarkMessage | None:
    """Replay a `merge_forward` envelope as one reportable text message.

    Returns a synthetic message that keeps the envelope's id/root/sender (the
    forwarder is the reporter in the watched group) but whose text is the merged
    transcript: each source message as "<name>：<text>" in chronological order.
    Later replies to the forward's topic keep the same root_id, so they append
    to the same issue once this root intakes.

    Media inside a forward is not fetchable — the source chat is usually one the
    bot is not a member of and Lark rejects the resource download — so each
    image/file becomes an inline marker instead of an Attachment. Returns None
    when there is nothing reportable (no inner content, or the expansion call
    fails and the message should be retried on the next scan).
    """
    try:
        items = lark.fetch_forwarded_messages(message_id=envelope.message_id)
    except Exception:  # noqa: BLE001 - expansion is best-effort; retry next scan
        return None
    inner = [
        item
        for item in items
        if isinstance(item, dict) and str(item.get("message_id") or "") != envelope.message_id
    ]
    if not inner:
        return None
    names = chat_member_names(lark, envelope.chat_id)
    parts: list[str] = []
    for item in inner[:_MAX_FORWARDED_ITEMS]:
        message = parse_lark_message(item, default_chat_id=envelope.chat_id)
        if message.msg_type in {"image", "media", "file", "audio"}:
            parts.append("[图片/附件（转发源会话不可访问，未自动拉取）]")
            continue
        text = (message.text or "").strip()
        if not text:
            continue
        name = names.get(message.sender_open_id) or ""
        parts.append((f"{name}：" if name else "") + text)
    if not parts:
        return None
    transcript = "\n".join(parts)
    if len(inner) > _MAX_FORWARDED_ITEMS:
        transcript += f"\n…（仅展开前 {_MAX_FORWARDED_ITEMS} 条，共 {len(inner)} 条）"
    return LarkMessage(
        message_id=envelope.message_id,
        chat_id=envelope.chat_id,
        root_id=envelope.root_id or envelope.message_id,
        sender_open_id=envelope.sender_open_id,
        sender_type=envelope.sender_type,
        create_time=envelope.create_time,
        msg_type="text",
        text=transcript,
        raw_content=json.dumps({"text": transcript}, ensure_ascii=False),
        sender_id=envelope.sender_id,
        sender_id_type=envelope.sender_id_type,
        deleted=False,
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
    root_allowlist: tuple[str, ...] = (),
    branch_tip_resolver: BranchTipResolver | None = None,
) -> BackfillResult:
    if resource_dir is not None and resource_store is not None:
        raise ValueError("resource_dir and resource_store are mutually exclusive")
    messages: list[LarkMessage] = []
    for chat_id in config.lark.all_chat_ids():
        messages.extend(lark.list_chat_messages(chat_id=chat_id, limit=limit))
    outcomes: list[IntakeOutcome] = []
    events: list[BackfillEvent] = []
    since_ms = config.intake.since_ms()
    allowed_roots = set(root_allowlist)
    store = resource_store
    if store is None and resource_dir is not None:
        store = LocalResourceStore(resource_dir)
    # Group workable messages by topic so a topic with many messages collapses
    # to one intake write instead of one comment/receipt per message.
    groups: dict[str, list[LarkMessage]] = {}
    for message in reversed(messages):
        if allowed_roots and (message.root_id or message.message_id) not in allowed_roots:
            events.append(
                BackfillEvent(
                    message_id=message.message_id,
                    action="skipped",
                    reason="not_in_root_allowlist",
                )
            )
            continue
        if (
            message.msg_type == MERGE_FORWARD_MSG_TYPE
            and processed_ledger is not None
            and processed_ledger.is_processed(message.message_id)
        ):
            events.append(
                BackfillEvent(message_id=message.message_id, action="skipped", reason="processed_ledger")
            )
            continue
        if message.msg_type == MERGE_FORWARD_MSG_TYPE:
            expanded = expand_merge_forward(lark, message)
            if expanded is None:
                events.append(
                    BackfillEvent(
                        message_id=message.message_id,
                        action="skipped",
                        reason="merge_forward_unexpandable",
                    )
                )
                continue
            message = expanded
        if should_skip_message(
            message,
            bot_open_id=config.lark.bot_open_id,
            bot_app_id=config.lark.app_id,
            since_ms=since_ms,
        ):
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
            events.append(
                BackfillEvent(
                    message_id=message.message_id,
                    action="skipped",
                    reason="processed_ledger",
                )
            )
            continue
        root_key = message.root_id or message.message_id
        groups.setdefault(root_key, []).append(message)
    for group in groups.values():
        outcome, group_events, processed_ids, _error = _process_message_group(
            group,
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
        events.extend(group_events)
        if outcome is not None:
            outcomes.append(outcome)
        if processed_ledger is not None:
            for message_id in processed_ids:
                processed_ledger.mark_processed(message_id)
    skipped = sum(1 for event in events if event.action == "skipped")
    return BackfillResult(
        scanned=len(messages),
        processed=len(outcomes),
        skipped=skipped,
        outcomes=tuple(outcomes),
        events=tuple(events),
    )


def _build_intake_record(
    message: LarkMessage,
    *,
    config: ProjectConfig,
    lark: LarkOpenApiMessengerClient,
    dry_run: bool = False,
    store: ResourceStore | None = None,
    resource_describer: ResourceDescriber | None = None,
    resource_policy: ResourcePolicy | None = None,
    resource_redactor: ResourceRedactor | None = None,
    resource_transformer: ResourceTransformer | None = None,
    branch_tip_resolver: BranchTipResolver | None = None,
) -> IntakeRecord | None:
    """Build the intake record for one message (branch resolve + materialize).

    Returns None for dry_run. Performs no GitHub write and no orphan check —
    those are decided per topic in `_process_message_group`.
    """
    target_branch = config.lark.branch_for_chat(message.chat_id)
    branch_tip_sha = ""
    if target_branch != "main" and branch_tip_resolver is not None:
        branch_tip_sha = branch_tip_resolver(target_branch)
    record = intake_record_from_lark_message(
        message,
        sender_names=config.lark.sender_names or {},
        member_names=chat_member_names(lark, message.chat_id),
        message_url_template=config.lark.message_url_template,
        target_branch=target_branch,
        branch_tip_sha=branch_tip_sha,
    )
    if dry_run:
        return None
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
    return record


def _process_message_group(
    messages: list[LarkMessage],
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
    branch_tip_resolver: BranchTipResolver | None = None,
    slash_handler: SlashCommandHandler | None = None,
) -> tuple[IntakeOutcome | None, list[BackfillEvent], list[str], str]:
    """Coalesce one topic's messages into a single intake write.

    Returns (outcome, events, processed_message_ids, error). A whole topic's
    messages collapse to one create or one combined follow-up comment, so a
    replayed backlog or a burst of replies produces one comment and one Lark
    receipt instead of one per message.
    """
    events: list[BackfillEvent] = []
    error = ""
    if not messages:
        return None, events, [], error
    # Deterministic slash commands (`/fix`, `/assign`) are executed literally and
    # peeled off before intake — they must not become issues or feed triage. Run
    # before the orphan check so `/fix` always gets a reply. Never in dry_run.
    processed_command_ids: list[str] = []
    if slash_handler is not None and not dry_run:
        remaining: list[LarkMessage] = []
        for message in messages:
            result = slash_handler.handle(message)
            if result is None:
                remaining.append(message)
                continue
            events.append(
                BackfillEvent(
                    message_id=message.message_id,
                    action="processed",
                    reason=result.reason,
                    issue_number=result.issue_number,
                )
            )
            processed_command_ids.append(message.message_id)
        messages = remaining
        if not messages:
            return None, events, processed_command_ids, error
    chat_id = messages[0].chat_id
    root_key = messages[0].root_id or messages[0].message_id
    if config.intake.skip_orphan_replies:
        contains_root = any(message.message_id == root_key for message in messages)
        try:
            has_issue = workflow.has_issue_for_root(chat_id=chat_id, root_id=root_key)
        except Exception as exc:  # noqa: BLE001 - a transient gh failure (e.g. api.github.com
            # EOF) inside the orphan probe must not kill the watcher: report it as
            # a topic error and retry the whole topic on the next scan.
            error = f"{type(exc).__name__}: {exc}"
            events.append(
                BackfillEvent(message_id=messages[0].message_id, action="error", reason=error)
            )
            return None, events, processed_command_ids, error
        if not contains_root and not has_issue:
            # Replies in a topic BugPatrol never intook and whose root is not in
            # this batch: appending is impossible and a fragment issue would be
            # misleading.
            events.extend(
                BackfillEvent(message_id=message.message_id, action="skipped", reason="orphan_reply")
                for message in messages
            )
            return None, events, processed_command_ids, error
    records: list[IntakeRecord] = []
    record_message_ids: list[str] = []
    for message in messages:
        try:
            record = _build_intake_record(
                message,
                config=config,
                lark=lark,
                dry_run=dry_run,
                store=store,
                resource_describer=resource_describer,
                resource_policy=resource_policy,
                resource_redactor=resource_redactor,
                resource_transformer=resource_transformer,
                branch_tip_resolver=branch_tip_resolver,
            )
        except Exception as exc:  # noqa: BLE001 - report and retry the rest next scan
            error = f"{type(exc).__name__}: {exc}"
            events.append(BackfillEvent(message_id=message.message_id, action="error", reason=error))
            break
        if record is None:
            events.append(BackfillEvent(message_id=message.message_id, action="skipped", reason="dry_run"))
            continue
        records.append(record)
        record_message_ids.append(message.message_id)
    if not records:
        return None, events, processed_command_ids, error
    try:
        outcome = workflow.process_batch(records)
    except Exception as exc:  # noqa: BLE001 - never marked processed, retries next scan
        error = f"{type(exc).__name__}: {exc}"
        events.append(BackfillEvent(message_id=records[0].message_id, action="error", reason=error))
        return None, events, processed_command_ids, error
    events.extend(
        BackfillEvent(
            message_id=message_id,
            action="processed",
            reason=outcome.action,
            issue_number=outcome.issue.number,
        )
        for message_id in record_message_ids
    )
    return outcome, events, [*processed_command_ids, *record_message_ids], error


def scan_topic_batches(
    *,
    config: ProjectConfig,
    lark: LarkOpenApiMessengerClient,
    limit: int = 20,
    processed_ledger: MessageLedger | None = None,
    exclude_roots: frozenset[str] = frozenset(),
    chat_id: str | None = None,
) -> ScanResult:
    """Cheap scan phase: filter messages and group the workable ones by topic.

    Scans `chat_id` (defaults to the main chat). Topics whose root key is in
    `exclude_roots` (already in flight on a worker) are left untouched so the
    next scan can pick them up again.
    """
    messages = lark.list_chat_messages(chat_id=chat_id or config.lark.chat_id, limit=limit)
    skipped_events: list[BackfillEvent] = []
    groups: dict[str, list[LarkMessage]] = {}
    since_ms = config.intake.since_ms()
    for message in reversed(messages):
        if (
            message.msg_type == MERGE_FORWARD_MSG_TYPE
            and processed_ledger is not None
            and processed_ledger.is_processed(message.message_id)
        ):
            skipped_events.append(
                BackfillEvent(message_id=message.message_id, action="skipped", reason="processed_ledger")
            )
            continue
        if message.msg_type == MERGE_FORWARD_MSG_TYPE:
            expanded = expand_merge_forward(lark, message)
            if expanded is None:
                skipped_events.append(
                    BackfillEvent(
                        message_id=message.message_id,
                        action="skipped",
                        reason="merge_forward_unexpandable",
                    )
                )
                continue
            message = expanded
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
    branch_tip_resolver: BranchTipResolver | None = None,
    slash_handler: SlashCommandHandler | None = None,
) -> TopicResult:
    """Coalesce one topic's messages into a single write. Never raises: an
    error stops the topic and is reported in the result so unprocessed messages
    retry on the next scan."""
    outcome, events, processed_ids, error = _process_message_group(
        list(batch.messages),
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
        slash_handler=slash_handler,
    )
    return TopicResult(
        root_key=batch.root_key,
        outcomes=(outcome,) if outcome is not None else (),
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
    member_names: dict[str, str] | None = None,
    message_url_template: str = "",
    target_branch: str = "main",
    branch_tip_sha: str = "",
) -> IntakeRecord:
    names = dict(sender_names or {})
    for open_id, name in (member_names or {}).items():
        names.setdefault(open_id, name)  # configured names win over API names
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
        target_branch=target_branch,
        branch_tip_sha=branch_tip_sha,
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
