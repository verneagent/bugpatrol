"""Local resource materialization for intake attachments."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.lark import DownloadedLarkResource

LARK_RESOURCE_RE = re.compile(r"^lark://message/([^/]+)/([^/]+)/([^/]+)$")


class LarkResourceDownloader(Protocol):
    def download_message_resource(self, *, message_id: str, resource_key: str) -> DownloadedLarkResource:
        """Download a Lark message resource."""


@dataclass(frozen=True)
class LarkResourceRef:
    message_id: str
    kind: str
    resource_key: str


class LocalResourceStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, *, ref: LarkResourceRef, resource: DownloadedLarkResource) -> Path:
        directory = self._root / _safe_segment(ref.message_id)
        directory.mkdir(parents=True, exist_ok=True)
        filename = _safe_segment(resource.filename or ref.resource_key)
        path = directory / filename
        path.write_bytes(resource.content)
        return path


def materialize_lark_attachments(
    *,
    record: IntakeRecord,
    lark: LarkResourceDownloader,
    store: LocalResourceStore,
) -> IntakeRecord:
    attachments = tuple(
        materialize_attachment(attachment=attachment, lark=lark, store=store)
        for attachment in record.attachments
    )
    return replace(record, attachments=attachments)


def materialize_attachment(
    *,
    attachment: Attachment,
    lark: LarkResourceDownloader,
    store: LocalResourceStore,
) -> Attachment:
    ref = parse_lark_resource_url(attachment.url)
    if ref is None:
        return attachment
    resource = lark.download_message_resource(
        message_id=ref.message_id,
        resource_key=ref.resource_key,
    )
    path = store.write(ref=ref, resource=resource)
    return Attachment(
        kind=attachment.kind,
        url=str(path),
        description=attachment.description or resource.filename,
    )


def parse_lark_resource_url(url: str) -> LarkResourceRef | None:
    match = LARK_RESOURCE_RE.match(url)
    if not match:
        return None
    return LarkResourceRef(
        message_id=match.group(1),
        kind=match.group(2),
        resource_key=match.group(3),
    )


def _safe_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe or "resource"
