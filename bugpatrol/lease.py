"""Local file lease for single-writer daemons."""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path


class LeaseHeldError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseInfo:
    owner: str
    acquired_at: float
    expires_at: float


class FileLease:
    def __init__(self, path: Path, *, ttl_seconds: float = 120, owner: str | None = None) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._path = path
        self._ttl_seconds = ttl_seconds
        self._owner = owner or default_lease_owner()
        self._held = False

    def acquire(self, *, now: float | None = None) -> LeaseInfo:
        current_time = time.time() if now is None else now
        self._path.parent.mkdir(parents=True, exist_ok=True)
        info = LeaseInfo(
            owner=self._owner,
            acquired_at=current_time,
            expires_at=current_time + self._ttl_seconds,
        )
        payload = _render_payload(info)
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = read_lease_info(self._path)
            if existing is not None and existing.expires_at <= current_time:
                self._path.unlink(missing_ok=True)
                return self.acquire(now=current_time)
            owner = existing.owner if existing is not None else "unknown"
            raise LeaseHeldError(f"watcher lease is held by {owner}")
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        self._held = True
        return info

    def refresh(self, *, now: float | None = None) -> LeaseInfo:
        if not self._held:
            raise LeaseHeldError("cannot refresh a lease that is not held")
        current_time = time.time() if now is None else now
        info = LeaseInfo(
            owner=self._owner,
            acquired_at=current_time,
            expires_at=current_time + self._ttl_seconds,
        )
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(_render_payload(info))
        os.replace(temp_path, self._path)
        return info

    def release(self) -> None:
        if self._held:
            self._path.unlink(missing_ok=True)
            self._held = False


def read_lease_info(path: Path) -> LeaseInfo | None:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return None
    owner = data.get("owner")
    acquired_at = data.get("acquired_at")
    expires_at = data.get("expires_at")
    if not isinstance(owner, str) or not isinstance(acquired_at, (int, float)) or not isinstance(expires_at, (int, float)):
        return None
    return LeaseInfo(owner=owner, acquired_at=float(acquired_at), expires_at=float(expires_at))


def default_lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _render_payload(info: LeaseInfo) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "owner": info.owner,
            "acquired_at": info.acquired_at,
            "expires_at": info.expires_at,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
