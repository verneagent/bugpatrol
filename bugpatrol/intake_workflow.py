"""Lark intake to GitHub issue workflow."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

from bugpatrol.clients import GitHubIssue, GitHubIssuesClient, LarkMessengerClient
from bugpatrol.config import ProjectConfig
from bugpatrol.fields import NATIVE_ISSUE_TYPES, validate_field_value
from bugpatrol.intake import (
    Attachment,
    IntakeRecord,
    format_created_at,
    parse_intake_metadata,
    render_attachments_markdown,
    render_batched_issue_body,
    render_issue_body,
)
from bugpatrol.lark import is_message_withdrawn_error
from bugpatrol.triage_queue import TriageSignal, classify_triage_signal

INTAKE_REPLY_META_MARKER = "BUGPATROL_INTAKE_REPLY_META"


def parse_intake_reply_metadata(body: str) -> dict[str, object] | None:
    """Parse the meta footer of an intake follow-up comment, if present."""
    marker = f"<!-- {INTAKE_REPLY_META_MARKER}:"
    start = body.find(marker)
    if start == -1:
        return None
    json_start = start + len(marker)
    end = body.find(" -->", json_start)
    if end == -1:
        return None
    data = json.loads(body[json_start:end])
    if not isinstance(data, dict):
        raise ValueError("intake reply metadata must be a JSON object")
    return data


def _collect_message_ids(meta: dict[str, object], into: set[str]) -> None:
    single = meta.get("message_id")
    if isinstance(single, str) and single:
        into.add(single)
    many = meta.get("message_ids")
    if isinstance(many, list):
        into.update(item for item in many if isinstance(item, str) and item)


@dataclass(frozen=True)
class IntakeOutcome:
    action: str
    issue: GitHubIssue
    lark_reply: str
    triage_signal: TriageSignal


class IssueFieldsClient(Protocol):
    def get_issue_field_values(self, *, repo: str, issue_number: int) -> dict[str, str]:
        """Return current GitHub Issue Field values keyed by GitHub field name."""

    def add_issue_field_values(self, **kwargs: object) -> None:
        """Write GitHub Issue Field values."""


class IntakeWorkflow:
    def __init__(
        self,
        *,
        config: ProjectConfig,
        github: GitHubIssuesClient,
        lark: LarkMessengerClient,
        issue_fields: IssueFieldsClient | None = None,
    ) -> None:
        self._config = config
        self._github = github
        self._lark = lark
        self._issue_fields = issue_fields

    def has_issue_for_root(self, *, chat_id: str, root_id: str) -> bool:
        return (
            self._github.find_issue_by_intake_root(
                repo=self._config.github_repo,
                chat_id=chat_id,
                root_id=root_id,
            )
            is not None
        )

    def process_batch(self, records: Sequence[IntakeRecord]) -> IntakeOutcome:
        """Coalesce one topic's messages into a single write.

        A watcher restart replaying a backlog — or a burst of replies between
        two scans — groups many messages of one topic into a single batch.
        Writing them one-by-one spams the issue with N comments and the Lark
        thread with N receipts. This folds the whole batch into one create (or
        one combined follow-up comment) and one Lark receipt. The single-record
        case delegates to `process` so live one-at-a-time intake is unchanged.
        """
        if not records:
            raise ValueError("process_batch requires at least one record")
        first = records[0]
        for record in records:
            if record.chat_id != first.chat_id or record.root_id != first.root_id:
                raise ValueError("process_batch records must all belong to one topic")
        if len(records) == 1:
            return self.process(first)
        if first.chat_id not in self._config.lark.all_chat_ids():
            raise ValueError(f"unexpected chat_id: {first.chat_id}")

        existing = self._github.find_issue_by_intake_root(
            repo=self._config.github_repo,
            chat_id=first.chat_id,
            root_id=first.root_id,
        )
        if existing is not None:
            if existing.state == "closed":
                return self._closed_outcome(record=records[-1], issue=existing)
            healed = self._heal_missing_intake_fields(record=first, issue_number=existing.number)
            recorded = self._recorded_message_ids(existing)
            new_records = [record for record in records if record.message_id not in recorded]
            if not new_records:
                # Every message in this batch was already captured — skip the
                # duplicate follow-up entirely (heal still applies above).
                return self._duplicate_outcome(record=records[-1], issue=existing, healed=healed)
            triage_signal = _merge_triage_signals(
                classify_triage_signal("updated", record, self._config.followup_classifier)
                for record in new_records
            )
            if len(new_records) == 1:
                comment = render_followup_comment(
                    new_records[0],
                    language=self._config.intake.language,
                    signal_reason=triage_signal.reason,
                )
            else:
                comment = render_batched_followup_comment(
                    new_records,
                    language=self._config.intake.language,
                    signal_reason=triage_signal.reason,
                )
            self._github.add_issue_comment(
                repo=self._config.github_repo,
                issue_number=existing.number,
                body=comment,
            )
            if healed and not triage_signal.should_enqueue:
                triage_signal = TriageSignal(
                    should_enqueue=True,
                    reason="healed_missing_fields",
                    material_message_ids=tuple(record.message_id for record in new_records),
                    asset_urls=_all_asset_urls(new_records),
                )
            self._mark_pending_after_final_status(
                issue_number=existing.number,
                triage_signal=triage_signal,
            )
            reply = self._reply_best_effort(
                record=new_records[-1],
                text=f"已追加 {len(new_records)} 条到 GitHub issue [#{existing.number}]({existing.url})",
            )
            return IntakeOutcome(
                action="updated",
                issue=existing,
                lark_reply=reply,
                triage_signal=triage_signal,
            )

        raced = self._github.find_issue_by_intake_root(
            repo=self._config.github_repo,
            chat_id=first.chat_id,
            root_id=first.root_id,
        )
        if raced is not None:
            reply = self._reply_best_effort(
                record=first,
                text=f"已创建 GitHub issue [#{raced.number}]({raced.url})",
            )
            return IntakeOutcome(
                action="deduplicated",
                issue=raced,
                lark_reply=reply,
                triage_signal=TriageSignal(should_enqueue=False, reason="create_race"),
            )
        fields = initial_intake_fields(first, include_branch=self._include_branch_field(first))
        # Evidence must reflect the whole batch, not just the first message.
        fields["Evidence"] = infer_evidence(
            _all_attachments(records),
            "\n".join(record.original_text for record in records),
        )
        validate_field_value("Evidence", fields["Evidence"])
        # Fold every message into the body so intake is one atomic create.
        issue = self._github.create_issue(
            repo=self._config.github_repo,
            title=build_issue_title(first),
            body=render_batched_issue_body(records, language=self._config.intake.language),
            issue_type="Bug",
            fields=fields,
        )
        reply = self._reply_best_effort(
            record=first,
            text=f"已创建 GitHub issue [#{issue.number}]({issue.url})",
        )
        return IntakeOutcome(
            action="created",
            issue=issue,
            lark_reply=reply,
            triage_signal=TriageSignal(
                should_enqueue=True,
                reason="intake_created",
                material_message_ids=tuple(record.message_id for record in records),
                asset_urls=_all_asset_urls(records),
            ),
        )

    def process(self, record: IntakeRecord) -> IntakeOutcome:
        if record.chat_id not in self._config.lark.all_chat_ids():
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
                # This message was already captured (a watcher replay or a
                # backfill re-scan): never append a second follow-up comment for
                # the same message.
                return self._duplicate_outcome(record=record, issue=existing, healed=healed)
            triage_signal = classify_triage_signal("updated", record, self._config.followup_classifier)
            comment = render_followup_comment(
                record,
                language=self._config.intake.language,
                signal_reason=triage_signal.reason,
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
            reply = self._reply_best_effort(
                record=record,
                text=f"已追加到 GitHub issue [#{existing.number}]({existing.url})",
            )
            return IntakeOutcome(
                action="updated",
                issue=existing,
                lark_reply=reply,
                triage_signal=triage_signal,
            )

        fields = initial_intake_fields(record, include_branch=self._include_branch_field(record))
        title = build_issue_title(record)
        body = render_issue_body(record, language=self._config.intake.language)
        raced = self._github.find_issue_by_intake_root(
            repo=self._config.github_repo,
            chat_id=record.chat_id,
            root_id=record.root_id,
        )
        if raced is not None:
            reply = self._reply_best_effort(
                record=record,
                text=f"已创建 GitHub issue [#{raced.number}]({raced.url})",
            )
            return IntakeOutcome(
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
        reply = self._reply_best_effort(
            record=record,
            text=f"已创建 GitHub issue [#{issue.number}]({issue.url})",
        )
        return IntakeOutcome(
            action="created",
            issue=issue,
            lark_reply=reply,
            triage_signal=classify_triage_signal("created", record, self._config.followup_classifier),
        )

    def _reply_best_effort(self, *, record: IntakeRecord, text: str) -> str:
        """Reply in the Lark thread; tolerate a withdrawn target message.

        The GitHub write has already happened by this point, so a recalled
        source message must not abort intake (it would crash-loop the watcher
        on a message that can never be replied to).
        """
        try:
            self._lark.reply_to_message(
                chat_id=record.chat_id,
                message_id=record.message_id,
                text=text,
            )
        except Exception as error:
            if not is_message_withdrawn_error(error):
                raise
            return f"{text}（原消息已撤回，未发送 Lark 回执）"
        return text

    def _recorded_message_ids(self, issue: GitHubIssue) -> set[str]:
        """Lark message ids already captured on an issue (body + follow-ups).

        The issue body's intake meta and every follow-up comment's meta embed
        the message ids they carry. Reading them back makes de-dup stateless:
        any re-scan (watcher replay or backfill) can tell what is already here
        without a local ledger.
        """
        recorded: set[str] = set()
        body_meta = parse_intake_metadata(issue.body or "")
        if body_meta is not None:
            _collect_message_ids(body_meta, recorded)
        for comment in self._github.list_issue_comments(
            repo=self._config.github_repo,
            issue_number=issue.number,
        ):
            reply_meta = parse_intake_reply_metadata(comment.body)
            if reply_meta is not None:
                _collect_message_ids(reply_meta, recorded)
        return recorded

    def _closed_outcome(
        self, *, record: IntakeRecord, issue: GitHubIssue
    ) -> IntakeOutcome:
        """A follow-up arrived on a closed issue: ignore it.

        A closed issue is a decided issue -- a plain reporter reply must not
        re-append comments or re-trigger triage on it. Only the explicit
        ``/reopen`` slash command (handled before intake) un-closes an issue. Tell
        the reporter once how to proceed and record nothing else.
        """
        reply = self._reply_best_effort(
            record=record,
            text=(
                f"该 issue [#{issue.number}]({issue.url}) 已关闭，本条回复未处理。"
                "如需重新处理，请在话题里发 /reopen（可在其后补充说明）。"
            ),
        )
        return IntakeOutcome(
            action="ignored_closed",
            issue=issue,
            lark_reply=reply,
            triage_signal=TriageSignal(should_enqueue=False, reason="issue_closed"),
        )

    def _duplicate_outcome(
        self, *, record: IntakeRecord, issue: GitHubIssue, healed: bool
    ) -> IntakeOutcome:
        """Outcome for a message already captured on the issue.

        No second comment and no Lark receipt (the message was already
        acknowledged when first captured). If the field-heal fired, triage still
        needs to run once, so enqueue that.
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
        return IntakeOutcome(
            action="duplicate",
            issue=issue,
            lark_reply="（该消息之前已处理，跳过重复追加）",
            triage_signal=triage_signal,
        )

    def _heal_missing_intake_fields(self, *, record: IntakeRecord, issue_number: int) -> bool:
        """Backfill intake fields lost to a crash between issue create and field write.

        A watcher crash in that window leaves an issue with no Triage status;
        the replayed message then dedupes into the followup path forever, so
        this is the only chance to repair the fields.
        """
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
            values=initial_intake_fields(record, include_branch=self._include_branch_field(record)),
            config=self._config,
        )
        return True

    def _include_branch_field(self, record: IntakeRecord) -> bool:
        """Write the free-text Branch mirror only for declared feature branches
        and only when the project maps a Branch field."""
        return record.target_branch != "main" and "Branch" in self._config.issue_field_names

    def _mark_pending_after_final_status(self, *, issue_number: int, triage_signal: TriageSignal) -> None:
        # A new topic reply re-enqueues triage; Pending reflects that the issue
        # is simply waiting for the next run — no human review needed.
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


def build_issue_title(record: IntakeRecord) -> str:
    text = " ".join(line.strip() for line in record.original_text.splitlines() if line.strip())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = "Lark bug report"
    if len(text) > 80:
        text = text[:77].rstrip() + "..."
    return f"[Lark] {text}"


def initial_intake_fields(record: IntakeRecord, *, include_branch: bool = False) -> dict[str, str]:
    fields = {
        "Source": "Lark",
        "Intake version": "v2",
        "Triage status": "Pending",
        "Evidence": infer_evidence(record.attachments, record.original_text),
    }
    for name, value in fields.items():
        validate_field_value(name, value)
    if "Bug" not in NATIVE_ISSUE_TYPES:
        raise ValueError("Bug issue type is not supported")
    # "Branch" is a free-text mirror (branches are dynamic, not an enum), so it
    # is added after enum validation. Only when the topic declares a feature
    # branch and the project maps the field.
    if include_branch:
        fields["Branch"] = record.target_branch
    return fields


def infer_evidence(attachments: tuple[Attachment, ...], original_text: str) -> str:
    evidence_types: set[str] = set()
    for item in attachments:
        kind = item.kind.lower()
        if "video" in kind:
            evidence_types.add("视频")
        elif "log" in kind:
            evidence_types.add("日志")
        elif "screenshot" in kind or "image" in kind or "photo" in kind:
            evidence_types.add("截图")
        elif item.url:
            evidence_types.add("多种")
    if len(evidence_types) > 1 or "多种" in evidence_types:
        return "多种"
    if evidence_types:
        return next(iter(evidence_types))
    if original_text.strip():
        return "文字描述"
    return "无"


def render_followup_comment(
    record: IntakeRecord, *, language: str = "en-US", signal_reason: str = ""
) -> str:
    copy = _followup_copy(language)
    attachments = render_attachments_markdown(record.attachments, copy=copy)
    meta = {
        "source": "lark",
        "schema_version": 1,
        "chat_id": record.chat_id,
        "root_id": record.root_id,
        "message_id": record.message_id,
        "reporter_open_id": record.reporter_open_id,
    }
    if signal_reason:
        # Record how the followup was classified so a later fix-revise can tell a
        # material correction from an ack/fix-status chatter without re-parsing text.
        meta["signal_reason"] = signal_reason
    return "\n".join(
        [
            f"## {copy['topic_update']}",
            "",
            f"- {copy['reporter']}: {record.reporter_name} ({record.reporter_open_id})",
            f"- {copy['created_at']}: {format_created_at(record.created_at)}",
            f"- {copy['lark_topic']}: {_link_or_id(label=copy['open_topic'], url=record.lark_topic_url, identifier=record.root_id)}",
            f"- {copy['message_id']}: {_link_or_id(label=copy['open_message'], url=record.lark_message_url, identifier=record.message_id)}",
            "",
            f"## {copy['message']}",
            "",
            record.original_text or copy["empty"],
            "",
            f"## {copy['attachments']}",
            "",
            attachments,
            "",
            "---",
            f"<!-- {INTAKE_REPLY_META_MARKER}:{json.dumps(meta, ensure_ascii=False, separators=(',', ':'))} -->",
        ]
    )


def render_batched_followup_comment(
    records: Sequence[IntakeRecord], *, language: str = "en-US", signal_reason: str = ""
) -> str:
    """One comment carrying several messages of the same topic.

    Each message keeps its own reporter/timestamp/attachments section, and a
    single meta footer lists every message id so the batch stays one comment.
    """
    if not records:
        raise ValueError("render_batched_followup_comment requires at least one record")
    copy = _followup_copy(language)
    first = records[0]
    lines: list[str] = [f"## {copy['topic_update']}（{len(records)}）", ""]
    for index, record in enumerate(records, start=1):
        attachments = render_attachments_markdown(record.attachments, copy=copy)
        lines.extend(
            [
                f"### {index}. {record.reporter_name} · {format_created_at(record.created_at)}",
                "",
                f"- {copy['message_id']}: {_link_or_id(label=copy['open_message'], url=record.lark_message_url, identifier=record.message_id)}",
                "",
                record.original_text or copy["empty"],
                "",
                f"**{copy['attachments']}**",
                "",
                attachments,
                "",
            ]
        )
    meta = {
        "source": "lark",
        "schema_version": 1,
        "chat_id": first.chat_id,
        "root_id": first.root_id,
        "message_ids": [record.message_id for record in records],
        "reporter_open_id": first.reporter_open_id,
    }
    if signal_reason:
        meta["signal_reason"] = signal_reason
    lines.extend(
        [
            "---",
            f"<!-- {INTAKE_REPLY_META_MARKER}:{json.dumps(meta, ensure_ascii=False, separators=(',', ':'))} -->",
        ]
    )
    return "\n".join(lines)


def _all_asset_urls(records: Sequence[IntakeRecord]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for record in records:
        for item in record.attachments:
            if item.url:
                seen.setdefault(item.url, None)
    return tuple(seen)


def _all_attachments(records: Sequence[IntakeRecord]) -> tuple[Attachment, ...]:
    return tuple(item for record in records for item in record.attachments)


def _merge_triage_signals(signals: Iterable[TriageSignal]) -> TriageSignal:
    signals = list(signals)
    should_enqueue = any(signal.should_enqueue for signal in signals)
    reason = next(
        (signal.reason for signal in signals if signal.should_enqueue),
        signals[0].reason if signals else "empty_followup",
    )
    material: dict[str, None] = {}
    assets: dict[str, None] = {}
    for signal in signals:
        for message_id in signal.material_message_ids:
            material.setdefault(message_id, None)
        for url in signal.asset_urls:
            assets.setdefault(url, None)
    return TriageSignal(
        should_enqueue=should_enqueue,
        reason=reason,
        material_message_ids=tuple(material),
        asset_urls=tuple(assets),
    )


def _followup_copy(language: str) -> dict[str, str]:
    if language == "zh-CN":
        return {
            "topic_update": "Lark 话题更新",
            "reporter": "上报人",
            "created_at": "创建时间",
            "lark_topic": "Lark 话题",
            "message_id": "消息 ID",
            "message": "消息",
            "attachments": "附件",
            "generated_description": "生成描述",
            "open_topic": "打开话题",
            "open_message": "打开消息",
            "open_asset": "打开附件",
            "preview": "预览",
            "image_alt": "图片",
            "none": "无",
            "empty": "（空）",
        }
    return {
        "topic_update": "Lark Topic Update",
        "reporter": "Reporter",
        "created_at": "Created at",
        "lark_topic": "Lark topic",
        "message_id": "Message id",
        "message": "Message",
        "attachments": "Attachments",
        "generated_description": "generated description",
        "open_topic": "open topic",
        "open_message": "open message",
        "open_asset": "open asset",
        "preview": "preview",
        "image_alt": "image",
        "none": "none",
        "empty": "(empty)",
    }


def _link_or_id(*, label: str, url: str, identifier: str) -> str:
    if url:
        return f"[{label}]({url}) (`{identifier}`)"
    return identifier
