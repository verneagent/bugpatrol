"""Intake rendering helpers.

The intake layer records facts. It does not decide whether a report is a code
bug, assign an owner, or compare behavior to PRD.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    lark_message_url: str = ""
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
        lark_message_url=str(data.get("lark_message_url") or ""),
        attachments=attachments,
    )


def render_issue_body(record: IntakeRecord, *, language: str = "en-US") -> str:
    copy = _copy(language)
    attachments = render_attachments_markdown(record.attachments, copy=copy)

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
            f"- {copy['created_at']}: {format_created_at(record.created_at)}",
            f"- {copy['lark_topic']}: {_link_or_id(label=copy['open_topic'], url=record.lark_topic_url, identifier=record.root_id)}",
            f"- {copy['message_id']}: {_link_or_id(label=copy['open_message'], url=record.lark_message_url, identifier=record.message_id)}",
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


def format_created_at(value: str) -> str:
    raw = value.strip()
    if not raw:
        return value
    try:
        timestamp = int(raw)
    except ValueError:
        return value
    if timestamp <= 0:
        return value
    seconds = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
    try:
        readable = datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, OverflowError, ValueError):
        return value
    return f"{readable} ({value})"


def render_attachments_markdown(attachments: tuple[Attachment, ...], *, copy: dict[str, str]) -> str:
    if not attachments:
        return f"- {copy['none']}"
    lines: list[str] = []
    for index, item in enumerate(attachments, start=1):
        url = item.url
        label = copy["open_asset"] if _is_url(url) else url
        lines.append(f"- {item.kind}: {_markdown_link(label, url) if _is_url(url) else url}")
        if _is_previewable_image(item):
            lines.append(f"  - {copy['preview']}:")
            lines.append("")
            lines.append(f"    ![{copy['image_alt']} {index}]({url})")
        if item.description:
            lines.append(f"  - {copy['generated_description']}: {item.description}")
    return "\n".join(lines)


def _is_previewable_image(item: Attachment) -> bool:
    return "image" in item.kind.lower() and _is_url(item.url)


def _is_url(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def _link_or_id(*, label: str, url: str, identifier: str) -> str:
    if url:
        return f"{_markdown_link(label, url)} (`{identifier}`)"
    return identifier


def _markdown_link(label: str, url: str) -> str:
    return f"[{label}]({url})"


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
            "open_topic": "打开话题",
            "open_message": "打开消息",
            "open_asset": "打开附件",
            "preview": "预览",
            "image_alt": "图片",
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
        "open_topic": "open topic",
        "open_message": "open message",
        "open_asset": "open asset",
        "preview": "preview",
        "image_alt": "image",
        "none": "none",
        "empty": "(empty)",
    }
