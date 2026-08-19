"""Polling public-mailbox (Lark Mail) intake watcher.

Mail reports in `bug@fivedegrees.ai` are mirrored into the same GitHub issue
pipeline as Lark topic reports. Because a customer thread cannot be replied to
(and we never auto-reply to customers), every notification lives in the
dedicated group: the first receipt posted there is recorded as the issue's
``notify_anchor_message_id`` and all later replies attach to it. See
docs/MAIL-INTAKE.md.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from bugpatrol.clients import GitHubIssue, GitHubIssuesClient, LarkMessengerClient
from bugpatrol.config import ProjectConfig
from bugpatrol.event_log import JsonlEventLog
from bugpatrol.intake import (
    Attachment,
    IntakeRecord,
    parse_intake_metadata,
    render_issue_body,
    resolve_reply_target,
    update_intake_metadata,
)
from bugpatrol.intake_workflow import (
    IssueFieldsClient,
    build_issue_title,
    collect_recorded_message_ids,
    initial_intake_fields,
    render_followup_comment,
)
from bugpatrol.lark import (
    DownloadedLarkResource,
    LarkOpenApiError,
    is_message_unreachable_error,
)
from bugpatrol.lease import FileLease
from bugpatrol.ledger import JsonMessageLedger, MessageLedger
from bugpatrol.mail import (
    LarkMailClient,
    MailAttachment,
    MailMessage,
    MailResourceDownloader,
)
from bugpatrol.resources import (
    LocalResourceStore,
    ResourceDescriber,
    ResourcePolicy,
    ResourceRedactor,
    ResourceStore,
    ResourceTransformer,
    materialize_attachment,
)
from bugpatrol.triage_queue import (
    CommandTriageDispatcher,
    TriageRequestQueue,
    TriageSignal,
    classify_triage_signal,
)
from bugpatrol.watcher import (
    MAX_CONSECUTIVE_SCAN_FAILURES,
    TriageDispatcher,
    TriageStatusReader,
    WatchResult,
    dispatch_due_triage,
    enqueue_triage_outcomes,
)


@dataclass(frozen=True)
class MailIntakeOutcome:
    action: str
    issue: GitHubIssue
    lark_reply: str
    triage_signal: TriageSignal


@dataclass(frozen=True)
class ScanMailResult:
    mails: tuple[MailMessage, ...]


# Attachment kinds that read like text evidence (logs, configs); anything else
# that is not an image/video is a generic file.
_MAIL_TEXT_EXTENSIONS = frozenset({".log", ".txt", ".json", ".csv", ".md", ".yaml", ".yml"})


class MailIntakeWorkflow:
    """Mail intake decisions: create/update the GitHub issue + group receipt.

    Mirrors ``IntakeWorkflow`` (Lark) except every notification is anchored to a
    group receipt instead of a customer thread, and de-dup keys on the mail
    ``message_id``.
    """

    def __init__(
        self,
        *,
        config: ProjectConfig,
        github: GitHubIssuesClient,
        lark: LarkMessengerClient,
        issue_fields: IssueFieldsClient | None = None,
    ) -> None:
        if config.mail is None:
            raise ValueError("config.mail is required for mail intake")
        self._config = config
        self._github = github
        self._lark = lark
        self._issue_fields = issue_fields

    def process(self, record: IntakeRecord) -> MailIntakeOutcome:
        """One mail message through intake; returns the outcome without raising."""
        if record.chat_id != self._config.mail.chat_id:
            raise ValueError(f"unexpected chat_id: {record.chat_id}")

        existing = self._github.find_issue_by_intake_root(
            repo=self._config.github_repo,
            chat_id=record.chat_id,
            root_id=record.root_id,
        )
        if existing is not None:
            if existing.state == "closed":
                return self._closed_outcome(record=record, issue=existing)
            healed = self._heal_missing_intake_fields(record=record, issue_number=existing.number)
            if record.message_id in self._recorded_message_ids(existing):
                # Already captured (a watcher replay or a backfill re-scan):
                # never append a second follow-up comment for the same mail.
                return self._duplicate_outcome(record=record, issue=existing, healed=healed)
            triage_signal = classify_triage_signal(
                "updated", record, self._config.followup_classifier
            )
            comment = render_followup_comment(
                record,
                language=self._config.intake.language,
                signal_reason=triage_signal.reason,
                source="mail",
            )
            self._github.add_issue_comment(
                repo=self._config.github_repo,
                issue_number=existing.number,
                body=comment,
            )
            if healed and not triage_signal.should_enqueue:
                # The issue was created but crashed before its fields were
                # written; make sure triage still runs at least once.
                triage_signal = TriageSignal(
                    should_enqueue=True,
                    reason="healed_missing_fields",
                    material_message_ids=(record.message_id,),
                    asset_urls=tuple(item.url for item in record.attachments if item.url),
                )
            self._mark_pending_after_final_status(
                issue_number=existing.number,
                triage_signal=triage_signal,
            )
            reply = self._notify(
                issue=existing,
                text=f"已追加到 GitHub issue [#{existing.number}]({existing.url})",
            )
            return MailIntakeOutcome(
                action="updated",
                issue=existing,
                lark_reply=reply,
                triage_signal=triage_signal,
            )

        fields = initial_intake_fields(record)
        title = build_issue_title(record, prefix="[邮件] ")
        body = render_issue_body(record, language=self._config.intake.language, source="mail")
        raced = self._github.find_issue_by_intake_root(
            repo=self._config.github_repo,
            chat_id=record.chat_id,
            root_id=record.root_id,
        )
        if raced is not None:
            reply = self._notify(
                issue=raced,
                text=f"已创建 GitHub issue [#{raced.number}]({raced.url})",
            )
            return MailIntakeOutcome(
                action="deduplicated",
                issue=raced,
                lark_reply=reply,
                triage_signal=TriageSignal(should_enqueue=False, reason="create_race"),
            )
        issue = self._github.create_issue(
            repo=self._config.github_repo,
            title=title,
            body=body,
            issue_type="Bug",
            fields=fields,
        )
        reply = self._notify(
            issue=issue,
            text=f"已创建 GitHub issue [#{issue.number}]({issue.url})",
        )
        return MailIntakeOutcome(
            action="created",
            issue=issue,
            lark_reply=reply,
            triage_signal=classify_triage_signal("created", record, self._config.followup_classifier),
        )

    def _notify(self, *, issue: GitHubIssue, text: str) -> str:
        """Post the group receipt (and anchor it) or reply to the existing anchor.

        Mail intake cannot reply to the customer thread, so every notification
        lives in the dedicated group. The first receipt becomes
        ``notify_anchor_message_id`` in the issue body meta and every later
        reply attaches to it. Idempotent: when the anchor is already present it
        replies to that receipt instead of posting a duplicate -- and a crash
        between issue create and the anchor patch is repaired right here on the
        next replay.
        """
        metadata = parse_intake_metadata(issue.body or "") or {}
        if metadata.get("notify_anchor_message_id"):
            return self._reply_to_anchor(issue=issue, text=text)
        sent = self._lark.send_chat_message(chat_id=self._config.mail.chat_id, text=text)
        self._github.update_issue_body(
            repo=self._config.github_repo,
            issue_number=issue.number,
            body=update_intake_metadata(issue.body or "", {"notify_anchor_message_id": sent.message_id}),
        )
        return text

    def _reply_to_anchor(self, *, issue: GitHubIssue, text: str) -> str:
        """Reply in the group on the anchored receipt; tolerate a lost receipt."""
        metadata = parse_intake_metadata(issue.body or "") or {}
        chat_id, message_id = resolve_reply_target(metadata)
        if not chat_id or not message_id:
            return f"{text}（未解析到群回执，未发送 Lark 通知）"
        try:
            self._lark.reply_to_message(chat_id=chat_id, message_id=message_id, text=text)
        except Exception as error:
            if not is_message_unreachable_error(error):
                raise
            return f"{text}（原回执已撤回或群已删除，未发送 Lark 通知）"
        return text

    def _closed_outcome(self, *, record: IntakeRecord, issue: GitHubIssue) -> MailIntakeOutcome:
        """A follow-up arrived on a closed issue: ignore it, but say so."""
        reply = self._notify(
            issue=issue,
            text=(
                f"该 issue [#{issue.number}]({issue.url}) 已关闭，本条邮件未处理。"
                "如需重新处理，请在群里发 /reopen。"
            ),
        )
        return MailIntakeOutcome(
            action="ignored_closed",
            issue=issue,
            lark_reply=reply,
            triage_signal=TriageSignal(should_enqueue=False, reason="issue_closed"),
        )

    def _duplicate_outcome(
        self, *, record: IntakeRecord, issue: GitHubIssue, healed: bool
    ) -> MailIntakeOutcome:
        """Outcome for a mail already captured on the issue.

        No second comment and no extra receipt (the mail was already
        acknowledged when first captured). If the field-heal fired, triage still
        needs to run once. A missing anchor is the one exception: it means the
        create crashed before the receipt was posted, so post it now.
        """
        if healed:
            triage_signal = TriageSignal(
                should_enqueue=True,
                reason="healed_missing_fields",
                material_message_ids=(record.message_id,),
                asset_urls=tuple(item.url for item in record.attachments if item.url),
            )
            self._mark_pending_after_final_status(
                issue_number=issue.number,
                triage_signal=triage_signal,
            )
        else:
            triage_signal = TriageSignal(should_enqueue=False, reason="duplicate_message")
        metadata = parse_intake_metadata(issue.body or "") or {}
        if metadata.get("notify_anchor_message_id"):
            reply = "（该邮件之前已处理，跳过重复追加）"
        else:
            reply = self._notify(
                issue=issue,
                text=f"已创建 GitHub issue [#{issue.number}]({issue.url})",
            )
        return MailIntakeOutcome(
            action="duplicate",
            issue=issue,
            lark_reply=reply,
            triage_signal=triage_signal,
        )

    def _recorded_message_ids(self, issue: GitHubIssue) -> set[str]:
        """Mail message ids already captured on an issue (body + follow-ups)."""
        return collect_recorded_message_ids(
            github=self._github,
            repo=self._config.github_repo,
            issue=issue,
        )

    def _heal_missing_intake_fields(self, *, record: IntakeRecord, issue_number: int) -> bool:
        """Backfill intake fields lost to a crash between issue create and field write."""
        if self._issue_fields is None:
            return False
        github_name = self._config.issue_field_names["Triage status"]
        values = self._issue_fields.get_issue_field_values(
            repo=self._config.github_repo,
            issue_number=issue_number,
        )
        if values.get(github_name):
            return False
        self._issue_fields.add_issue_field_values(
            repo=self._config.github_repo,
            issue_number=issue_number,
            values=initial_intake_fields(record),
            config=self._config,
        )
        return True

    def _mark_pending_after_final_status(self, *, issue_number: int, triage_signal: TriageSignal) -> None:
        if self._issue_fields is None or not triage_signal.should_enqueue:
            return
        github_name = self._config.issue_field_names["Triage status"]
        values = self._issue_fields.get_issue_field_values(
            repo=self._config.github_repo,
            issue_number=issue_number,
        )
        if values.get(github_name) not in {"Done", "Skipped"}:
            return
        self._issue_fields.add_issue_field_values(
            repo=self._config.github_repo,
            issue_number=issue_number,
            values={"Triage status": "Pending"},
            config=self._config,
        )


def build_mail_record(
    *,
    config: ProjectConfig,
    mail: MailMessage,
    body_text: str,
    attachments: tuple[Attachment, ...],
) -> IntakeRecord:
    """Map a mail message onto the shared intake record shape.

    The mail thread_id groups replies into one issue, the mail message_id is the
    de-dup key, and the reporter is identified by ``mail:<address>`` (or a
    configured internal address -> Lark open_id). The subject leads the text so
    the issue title derives from it.
    """
    mail_config = config.mail
    if mail_config is None:
        raise ValueError("config.mail is required for mail intake")
    address = mail.sender_address
    open_id = next(
        (
            open_id
            for email, open_id in (mail_config.user_emails or {}).items()
            if email.lower() == address
        ),
        None,
    )
    if not open_id:
        open_id = f"mail:{address}"
    parts: list[str] = []
    if mail.subject.strip():
        parts.append(mail.subject.strip())
    if body_text.strip():
        parts.append(body_text.strip())
    return IntakeRecord(
        reporter_name=mail.head_from.name or address,
        reporter_open_id=open_id,
        created_at=str(mail.internal_date_ms),
        chat_id=mail_config.chat_id,
        root_id=mail.thread_id,
        message_id=mail.message_id,
        original_text="\n\n".join(parts),
        attachments=attachments,
    )


def _mail_attachment_kind(attachment: MailAttachment) -> str:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return "screenshot"
    if content_type.startswith("video/"):
        return "video"
    if (attachment.filename or "").lower().endswith(tuple(_MAIL_TEXT_EXTENSIONS)):
        return "log"
    return "file"


def _mail_attachment_url(message_id: str, attachment_id: str) -> str:
    # Mail message_ids contain '/' and '=', so both segments are percent-encoded
    # to keep the synthetic lark:// message URL structurally parseable. The
    # downloader adapter decodes them back before calling the mail API.
    return (
        f"lark://message/{quote(message_id, safe='')}/"
        f"mail/{quote(attachment_id, safe='')}"
    )


class _MailResourceDownloaderAdapter:
    """Bridge a mail attachment download to the lark resource protocol.

    Lets ``materialize_attachment`` run unchanged for mail: the same redact /
    transform / policy / describe / store pipeline applies to mail attachments.
    """

    def __init__(self, *, mailbox: str, mail: MailResourceDownloader) -> None:
        self._mailbox = mailbox
        self._mail = mail

    def download_message_resource(
        self, *, message_id: str, resource_key: str, resource_type: str
    ) -> DownloadedLarkResource:
        return self._mail.download_attachment(
            mailbox=self._mailbox,
            message_id=unquote(message_id),
            attachment_id=unquote(resource_key),
        )


def materialize_mail_attachments(
    *,
    mailbox: str,
    message_id: str,
    mail_attachments: tuple[MailAttachment, ...],
    mail: MailResourceDownloader,
    store: ResourceStore,
    describer: ResourceDescriber | None = None,
    policy: ResourcePolicy | None = None,
    redactor: ResourceRedactor | None = None,
    transformer: ResourceTransformer | None = None,
) -> tuple[Attachment, ...]:
    """Download non-inline mail attachments through the shared resource pipeline.

    Inline images (signature logos, quoted-history screenshots) are decoration,
    not evidence, so they are skipped. Each real attachment materializes to a
    stored path (or the policy rejection text), exactly like a Lark attachment.
    """
    adapter = _MailResourceDownloaderAdapter(mailbox=mailbox, mail=mail)
    result: list[Attachment] = []
    for item in mail_attachments:
        if item.is_inline:
            continue
        attachment = Attachment(
            kind=_mail_attachment_kind(item),
            url=_mail_attachment_url(message_id, item.attachment_id),
            description=item.filename,
        )
        result.append(
            materialize_attachment(
                attachment=attachment,
                lark=adapter,
                store=store,
                describer=describer,
                policy=policy,
                redactor=redactor,
                transformer=transformer,
            )
        )
    return tuple(result)


def _scan_mail(
    *,
    config: ProjectConfig,
    mail: LarkMailClient,
    limit: int,
    processed_ledger: MessageLedger | None,
    skip_self_sent: bool = True,
) -> ScanMailResult:
    """List unprocessed INBOX mail, skipping ledgered and self-sent items.

    ``page_size`` is capped at 20 by the mail API, so a ``limit`` above 20
    paginates. Self-sent items (sender == the mailbox itself, e.g. delivery
    notifications or test mail) are never reports and are skipped.
    """
    mail_config = config.mail
    if mail_config is None:
        raise ValueError("config.mail is required for mail intake")
    seen: list[MailMessage] = []
    has_more = True
    page_token = ""
    self_address = mail_config.mailbox.lower()
    while has_more and len(seen) < limit:
        items, has_more, page_token = mail.list_messages(
            mailbox=mail_config.mailbox,
            page_size=min(20, max(1, limit - len(seen))),
            page_token=page_token,
        )
        for item in items:
            if len(seen) >= limit:
                has_more = False
                break
            if processed_ledger is not None and processed_ledger.is_processed(item.message_id):
                continue
            if skip_self_sent and item.sender_address == self_address:
                continue
            seen.append(item)
        if not page_token:
            break
    return ScanMailResult(mails=tuple(seen))


def run_mail_watcher(
    *,
    config: ProjectConfig,
    mail: LarkMailClient,
    workflow: MailIntakeWorkflow,
    limit: int = 20,
    interval_seconds: float = 60,
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
) -> WatchResult:
    """Poll the configured mailbox and drive intake until stopped.

    Mirrors ``run_polling_watcher``'s contract: transient scan failures retry up
    to ``MAX_CONSECUTIVE_SCAN_FAILURES`` then crash (so launchd/operators see a
    persistent outage); a per-message fetch or processing failure is logged
    loudly and left un-ledgered so it retries next poll. Returns aggregated
    counts as a ``WatchResult``.
    """
    if config.mail is None:
        raise ValueError("config.mail is required for mail intake")
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
    store = resource_store
    if store is None and resource_dir is not None:
        store = LocalResourceStore(resource_dir)

    iterations = 0
    scanned = 0
    processed = 0
    skipped = 0
    queued_triage = 0
    dispatched_triage = 0
    if lease is not None:
        lease.acquire()
    try:
        consecutive_scan_failures = 0
        while True:
            iterations += 1
            try:
                mail_scan = _scan_mail(
                    config=config,
                    mail=mail,
                    limit=limit,
                    processed_ledger=ledger,
                )
            except LarkOpenApiError as error:
                # Transient mail/network failures (timeouts, expired tokens)
                # must not kill the watcher; retry next poll. Persistent
                # failures still crash so launchd/operators see them.
                consecutive_scan_failures += 1
                if consecutive_scan_failures >= MAX_CONSECUTIVE_SCAN_FAILURES:
                    raise
                print(
                    f"watch-mail: scan failed ({consecutive_scan_failures}/"
                    f"{MAX_CONSECUTIVE_SCAN_FAILURES}), retrying next poll: {error}",
                    file=sys.stderr,
                )
                if logger is not None:
                    logger.write(
                        {
                            "event": "mail_scan_error",
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
            iteration_outcomes: list[MailIntakeOutcome] = []
            iteration_skipped = 0
            for mail_msg in mail_scan.mails:
                try:
                    full = mail.get_message(
                        mailbox=config.mail.mailbox,
                        message_id=mail_msg.message_id,
                    )
                except LarkOpenApiError as error:
                    # One message failing to fetch is not a scan outage: log it
                    # loudly and leave it un-ledgered so it retries next poll.
                    skipped += 1
                    iteration_skipped += 1
                    print(
                        f"watch-mail: fetch failed for {mail_msg.message_id}: {error}",
                        file=sys.stderr,
                    )
                    if logger is not None:
                        logger.write(
                            {
                                "event": "mail_message",
                                "iteration": iterations,
                                "message_id": mail_msg.message_id,
                                "action": "error",
                                "error": str(error),
                            }
                        )
                    continue
                attachments: tuple[Attachment, ...] = ()
                if store is not None:
                    attachments = materialize_mail_attachments(
                        mailbox=config.mail.mailbox,
                        message_id=full.message_id,
                        mail_attachments=full.attachments,
                        mail=mail,
                        store=store,
                        describer=resource_describer,
                        policy=resource_policy,
                        redactor=resource_redactor,
                        transformer=resource_transformer,
                    )
                record = build_mail_record(
                    config=config,
                    mail=full,
                    body_text=full.body_plain_text,
                    attachments=attachments,
                )
                if dry_run:
                    processed += 1
                    iteration_skipped += 1
                    if logger is not None:
                        logger.write(
                            {
                                "event": "mail_message",
                                "iteration": iterations,
                                "message_id": full.message_id,
                                "action": "dry_run",
                            }
                        )
                    continue
                try:
                    outcome = workflow.process(record)
                except Exception as error:
                    skipped += 1
                    iteration_skipped += 1
                    print(
                        f"watch-mail: processing failed for {full.message_id}: "
                        f"{type(error).__name__}: {error}",
                        file=sys.stderr,
                    )
                    if logger is not None:
                        logger.write(
                            {
                                "event": "mail_message",
                                "iteration": iterations,
                                "message_id": full.message_id,
                                "action": "error",
                                "error": f"{type(error).__name__}: {error}",
                            }
                        )
                    continue
                iteration_outcomes.append(outcome)
                if ledger is not None:
                    ledger.mark_processed(full.message_id)
                if logger is not None:
                    logger.write(
                        {
                            "event": "mail_message",
                            "iteration": iterations,
                            "message_id": full.message_id,
                            "action": outcome.action,
                            "issue_number": outcome.issue.number,
                        }
                    )
            scanned += len(mail_scan.mails)
            processed += len(iteration_outcomes)
            if logger is not None:
                logger.write(
                    {
                        "event": "mail_scan",
                        "iteration": iterations,
                        "scanned": len(mail_scan.mails),
                        "processed": len(iteration_outcomes),
                        "skipped": iteration_skipped,
                    }
                )
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
