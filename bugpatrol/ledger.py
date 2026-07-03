"""Durable processed-message ledger."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol


class MessageLedger(Protocol):
    def is_processed(self, message_id: str) -> bool:
        """Return whether a message has already been processed."""

    def mark_processed(self, message_id: str) -> None:
        """Record a successfully processed message."""


class JsonMessageLedger:
    def __init__(self, path: Path, *, processed_message_ids: set[str] | None = None) -> None:
        self._path = path
        self._processed_message_ids = set(processed_message_ids or set())

    @classmethod
    def load(cls, path: Path) -> "JsonMessageLedger":
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("unsupported message ledger file")
        raw_ids = data.get("processed_message_ids") or []
        if not isinstance(raw_ids, list) or not all(isinstance(item, str) for item in raw_ids):
            raise ValueError("processed_message_ids must be a string list")
        return cls(path, processed_message_ids=set(raw_ids))

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._processed_message_ids

    def mark_processed(self, message_id: str) -> None:
        if message_id in self._processed_message_ids:
            return
        self._processed_message_ids.add(message_id)
        self.save()

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "processed_message_ids": sorted(self._processed_message_ids),
        }
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        os.replace(temp_path, self._path)
