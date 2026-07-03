"""Structured JSONL event log."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


class JsonlEventLog:
    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, event: dict[str, Any] | object) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(event) if is_dataclass(event) else event
        with self._path.open("a") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
