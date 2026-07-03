"""Lark intake to GitHub issue workflow."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from bugpatrol.clients import GitHubIssue, GitHubIssuesClient, LarkMessengerClient
from bugpatrol.config import ProjectConfig
from bugpatrol.fields import NATIVE_ISSUE_TYPES, validate_field_value
from bugpatrol.intake import Attachment, IntakeRecord, render_issue_body
from bugpatrol.triage_queue import TriageSignal, classify_triage_signal

INTAKE_REPLY_META_MARKER = "BUGPATROL_INTAKE_REPLY_META"


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

    def process(self, record: IntakeRecord) -> IntakeOutcome:
        if record.chat_id != self._config.lark.chat_id:
            raise ValueError(f"unexpected chat_id: {record.chat_id}")

        existing = self._github.find_issue_by_intake_root(
            repo=self._config.github_repo,
            chat_id=record.chat_id,
            root_id=record.root_id,
        )
        if existing is not None:
            comment = render_followup_comment(record, language=self._config.intake.language)
            self._github.add_issue_comment(
                repo=self._config.github_repo,
                issue_number=existing.number,
                body=comment,
            )
            triage_signal = classify_triage_signal("updated", record, self._config.followup_classifier)
            self._mark_needs_review_after_final_status(
                issue_number=existing.number,
                triage_signal=triage_signal,
            )
            reply = f"已追加到 GitHub issue #{existing.number}: {existing.url}"
            self._lark.reply_to_message(
                chat_id=record.chat_id,
                message_id=record.message_id,
                text=reply,
            )
            return IntakeOutcome(
                action="updated",
                issue=existing,
                lark_reply=reply,
                triage_signal=triage_signal,
            )

        fields = initial_intake_fields(record)
        title = build_issue_title(record)
        body = render_issue_body(record, language=self._config.intake.language)
        raced = self._github.find_issue_by_intake_root(
            repo=self._config.github_repo,
            chat_id=record.chat_id,
            root_id=record.root_id,
        )
        if raced is not None:
            reply = f"已创建 GitHub issue #{raced.number}: {raced.url}"
            self._lark.reply_to_message(chat_id=record.chat_id, message_id=record.message_id, text=reply)
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
        reply = f"已创建 GitHub issue #{issue.number}: {issue.url}"
        self._lark.reply_to_message(chat_id=record.chat_id, message_id=record.message_id, text=reply)
        return IntakeOutcome(
            action="created",
            issue=issue,
            lark_reply=reply,
            triage_signal=classify_triage_signal("created", record, self._config.followup_classifier),
        )

    def _mark_needs_review_after_final_status(self, *, issue_number: int, triage_signal: TriageSignal) -> None:
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
            values={"Triage status": "Needs review"},
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


def initial_intake_fields(record: IntakeRecord) -> dict[str, str]:
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


def render_followup_comment(record: IntakeRecord, *, language: str = "en-US") -> str:
    copy = _followup_copy(language)
    attachment_lines = []
    for item in record.attachments:
        line = f"- {item.kind}: {item.url}"
        if item.description:
            line += f"\n  - {copy['generated_description']}: {item.description}"
        attachment_lines.append(line)
    attachments = "\n".join(attachment_lines) if attachment_lines else f"- {copy['none']}"
    meta = {
        "source": "lark",
        "schema_version": 1,
        "chat_id": record.chat_id,
        "root_id": record.root_id,
        "message_id": record.message_id,
        "reporter_open_id": record.reporter_open_id,
    }
    return "\n".join(
        [
            f"## {copy['topic_update']}",
            "",
            f"- {copy['reporter']}: {record.reporter_name} ({record.reporter_open_id})",
            f"- {copy['created_at']}: {record.created_at}",
            f"- {copy['message_id']}: {record.message_id}",
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


def _followup_copy(language: str) -> dict[str, str]:
    if language == "zh-CN":
        return {
            "topic_update": "Lark 话题更新",
            "reporter": "上报人",
            "created_at": "创建时间",
            "message_id": "消息 ID",
            "message": "消息",
            "attachments": "附件",
            "generated_description": "生成描述",
            "none": "无",
            "empty": "（空）",
        }
    return {
        "topic_update": "Lark Topic Update",
        "reporter": "Reporter",
        "created_at": "Created at",
        "message_id": "Message id",
        "message": "Message",
        "attachments": "Attachments",
        "generated_description": "generated description",
        "none": "none",
        "empty": "(empty)",
    }
