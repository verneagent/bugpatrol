"""Intake rendering helpers.

The intake layer records facts. It does not decide whether a report is a code
bug, assign an owner, or compare behavior to PRD.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from bugpatrol.clients import GitHubIssue

INTAKE_META_MARKER = "BUGPATROL_INTAKE_META"


@dataclass(frozen=True)
class Attachment:
    kind: str
    url: str
    description: str = ""


@dataclass(frozen=True)
class IntakeRecord:
    reporter_name: str
    reporter_open_id: str
    created_at: str
    chat_id: str
    root_id: str
    message_id: str
    original_text: str
    lark_topic_url: str = ""
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)


def intake_record_from_dict(data: dict[str, Any]) -> IntakeRecord:
    raw_attachments = data.get("attachments", ())
    if not isinstance(raw_attachments, (list, tuple)):
        raise ValueError("attachments must be a list")
    attachments = tuple(
        Attachment(
            kind=_required_str(_required_dict(item, "attachment"), "kind"),
            url=_required_str(_required_dict(item, "attachment"), "url"),
            description=str(_required_dict(item, "attachment").get("description") or ""),
        )
        for item in raw_attachments
    )
    return IntakeRecord(
        reporter_name=_required_str(data, "reporter_name"),
        reporter_open_id=_required_str(data, "reporter_open_id"),
        created_at=_required_str(data, "created_at"),
        chat_id=_required_str(data, "chat_id"),
        root_id=_required_str(data, "root_id"),
        message_id=_required_str(data, "message_id"),
        original_text=str(data.get("original_text") or ""),
        lark_topic_url=str(data.get("lark_topic_url") or ""),
        attachments=attachments,
    )


def render_issue_body(record: IntakeRecord, *, language: str = "en-US") -> str:
    copy = _copy(language)
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
        "attachment_urls": [item.url for item in record.attachments],
    }

    return "\n".join(
        [
            f"## {copy['lark_intake']}",
            "",
            f"- {copy['reporter']}: {record.reporter_name} ({record.reporter_open_id})",
            f"- {copy['created_at']}: {record.created_at}",
            f"- {copy['lark_topic']}: {record.lark_topic_url or record.root_id}",
            f"- {copy['message_id']}: {record.message_id}",
            "",
            f"## {copy['original_message']}",
            "",
            record.original_text or copy["empty"],
            "",
            f"## {copy['attachments']}",
            "",
            attachments,
            "",
            "---",
            f"<!-- {INTAKE_META_MARKER}:{json.dumps(meta, ensure_ascii=False, separators=(',', ':'))} -->",
        ]
    )


def parse_intake_metadata(body: str) -> dict[str, Any] | None:
    marker = f"<!-- {INTAKE_META_MARKER}:"
    start = body.find(marker)
    if start == -1:
        return None
    json_start = start + len(marker)
    end = body.find(" -->", json_start)
    if end == -1:
        return None
    data = json.loads(body[json_start:end])
    if not isinstance(data, dict):
        raise ValueError("intake metadata must be a JSON object")
    return data


def is_bugpatrol_managed_issue(issue: GitHubIssue) -> bool:
    """Return true when an issue was created by BugPatrol intake."""
    return parse_intake_metadata(issue.body or "") is not None


def require_bugpatrol_managed_issue(issue: GitHubIssue) -> dict[str, Any]:
    metadata = parse_intake_metadata(issue.body or "")
    if metadata is None:
        raise ValueError(
            f"issue #{issue.number} is not managed by BugPatrol: missing {INTAKE_META_MARKER}"
        )
    return metadata


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing string field {key!r}")
    return value


def _required_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _copy(language: str) -> dict[str, str]:
    if language == "zh-CN":
        return {
            "lark_intake": "Lark 上报",
            "reporter": "上报人",
            "created_at": "创建时间",
            "lark_topic": "Lark 话题",
            "message_id": "消息 ID",
            "original_message": "原始消息",
            "attachments": "附件",
            "generated_description": "生成描述",
            "none": "无",
            "empty": "（空）",
        }
    return {
        "lark_intake": "Lark Intake",
        "reporter": "Reporter",
        "created_at": "Created at",
        "lark_topic": "Lark topic",
        "message_id": "Message id",
        "original_message": "Original Message",
        "attachments": "Attachments",
        "generated_description": "generated description",
        "none": "none",
        "empty": "(empty)",
    }
