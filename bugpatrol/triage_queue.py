"""Debounced triage request queue."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from bugpatrol.config import FollowupClassifierConfig
from bugpatrol.intake import IntakeRecord


@dataclass(frozen=True)
class TriageSignal:
    should_enqueue: bool
    reason: str
    material_message_ids: tuple[str, ...] = ()
    asset_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriageRequest:
    issue_number: int
    due_at: float
    reasons: tuple[str, ...]
    material_message_ids: tuple[str, ...]
    asset_urls: tuple[str, ...]
    trigger_fingerprint: str
    updated_at: float
    pending_review: bool = False


@dataclass(frozen=True)
class DispatchResult:
    issue_number: int
    trigger_fingerprint: str
    command: tuple[str, ...]


ACK_TEXTS = {
    "ok",
    "okay",
    "好的",
    "好",
    "收到",
    "谢谢",
    "感谢",
    "明白",
    "了解",
}

FIX_STATUS_KEYWORDS = (
    "fixed",
    "fix",
    "merged",
    "closed",
    "pr ",
    "pull request",
    "已修",
    "修了",
    "合并",
    "已合",
    "关闭",
)


def classify_triage_signal(
    action: str,
    record: IntakeRecord,
    config: FollowupClassifierConfig | None = None,
) -> TriageSignal:
    asset_urls = tuple(item.url for item in record.attachments if item.url)
    if action == "created":
        return TriageSignal(
            should_enqueue=True,
            reason="intake_created",
            material_message_ids=(record.message_id,),
            asset_urls=asset_urls,
        )

    if asset_urls:
        return TriageSignal(
            should_enqueue=True,
            reason="material_followup",
            material_message_ids=(record.message_id,),
            asset_urls=asset_urls,
        )

    text = " ".join(record.original_text.split()).strip()
    if not text:
        return TriageSignal(should_enqueue=False, reason="empty_followup")

    ack_texts = set(ACK_TEXTS)
    if config is not None:
        ack_texts.update(item.lower() for item in config.acknowledgement_texts)
    if text.lower() in ack_texts:
        return TriageSignal(should_enqueue=False, reason="acknowledgement")

    lowered = f" {text.lower()} "
    fix_keywords = list(FIX_STATUS_KEYWORDS)
    if config is not None:
        fix_keywords.extend(item.lower() for item in config.fix_status_keywords)
    if any(keyword in lowered for keyword in fix_keywords):
        return TriageSignal(should_enqueue=False, reason="fix_status_chatter")

    return TriageSignal(
        should_enqueue=True,
        reason="material_followup",
        material_message_ids=(record.message_id,),
        asset_urls=asset_urls,
    )


class TriageRequestQueue:
    def __init__(self, path: Path, *, requests: dict[int, TriageRequest] | None = None) -> None:
        self._path = path
        self._requests = dict(requests or {})

    @classmethod
    def load(cls, path: Path) -> "TriageRequestQueue":
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("unsupported triage queue file")
        requests: dict[int, TriageRequest] = {}
        raw_requests = data.get("requests") or []
        if not isinstance(raw_requests, list):
            raise ValueError("triage queue requests must be a list")
        for item in raw_requests:
            if not isinstance(item, dict):
                raise ValueError("triage queue request must be an object")
            request = TriageRequest(
                issue_number=int(item["issue_number"]),
                due_at=float(item["due_at"]),
                reasons=tuple(str(value) for value in item.get("reasons", ())),
                material_message_ids=tuple(str(value) for value in item.get("material_message_ids", ())),
                asset_urls=tuple(str(value) for value in item.get("asset_urls", ())),
                trigger_fingerprint=str(item["trigger_fingerprint"]),
                updated_at=float(item["updated_at"]),
                pending_review=bool(item.get("pending_review") or False),
            )
            requests[request.issue_number] = request
        return cls(path, requests=requests)

    def enqueue(
        self,
        *,
        issue_number: int,
        signal: TriageSignal,
        quiet_seconds: float,
        now: float | None = None,
    ) -> TriageRequest | None:
        if not signal.should_enqueue:
            return None
        current_time = time.time() if now is None else now
        existing = self._requests.get(issue_number)
        reasons = _merge_unique(existing.reasons if existing else (), (signal.reason,))
        material_message_ids = _merge_unique(
            existing.material_message_ids if existing else (),
            signal.material_message_ids,
        )
        asset_urls = _merge_unique(existing.asset_urls if existing else (), signal.asset_urls)
        request = TriageRequest(
            issue_number=issue_number,
            due_at=current_time + quiet_seconds,
            reasons=reasons,
            material_message_ids=material_message_ids,
            asset_urls=asset_urls,
            trigger_fingerprint=triage_trigger_fingerprint(
                issue_number=issue_number,
                material_message_ids=material_message_ids,
                asset_urls=asset_urls,
            ),
            updated_at=current_time,
            pending_review=existing.pending_review if existing else False,
        )
        self._requests[issue_number] = request
        self.save()
        return request

    def mark_pending_review(
        self,
        *,
        request: TriageRequest,
        quiet_seconds: float,
        now: float | None = None,
    ) -> TriageRequest:
        current_time = time.time() if now is None else now
        updated = TriageRequest(
            issue_number=request.issue_number,
            due_at=current_time + quiet_seconds,
            reasons=_merge_unique(request.reasons, ("pending_review_running",)),
            material_message_ids=request.material_message_ids,
            asset_urls=request.asset_urls,
            trigger_fingerprint=request.trigger_fingerprint,
            updated_at=current_time,
            pending_review=True,
        )
        self._requests[request.issue_number] = updated
        self.save()
        return updated

    def due_requests(self, *, now: float | None = None) -> tuple[TriageRequest, ...]:
        current_time = time.time() if now is None else now
        return tuple(
            request
            for request in sorted(self._requests.values(), key=lambda item: (item.due_at, item.issue_number))
            if request.due_at <= current_time
        )

    def mark_dispatched(self, request: TriageRequest) -> None:
        current = self._requests.get(request.issue_number)
        if current and current.trigger_fingerprint == request.trigger_fingerprint:
            del self._requests[request.issue_number]
            self.save()

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "requests": [
                {
                    "issue_number": request.issue_number,
                    "due_at": request.due_at,
                    "reasons": list(request.reasons),
                    "material_message_ids": list(request.material_message_ids),
                    "asset_urls": list(request.asset_urls),
                    "trigger_fingerprint": request.trigger_fingerprint,
                    "updated_at": request.updated_at,
                    "pending_review": request.pending_review,
                }
                for request in sorted(self._requests.values(), key=lambda item: item.issue_number)
            ],
        }
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        os.replace(temp_path, self._path)


class CommandTriageDispatcher:
    def __init__(self, command_template: str | Sequence[str]) -> None:
        if isinstance(command_template, str):
            self._command_template = tuple(shlex.split(command_template))
        elif len(command_template) == 1 and isinstance(command_template[0], str):
            self._command_template = tuple(shlex.split(command_template[0]))
        else:
            self._command_template = tuple(command_template)
        if not self._command_template:
            raise ValueError("triage dispatch command must not be empty")

    def dispatch(self, request: TriageRequest) -> DispatchResult:
        command = tuple(_format_command_part(part, request) for part in self._command_template)
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"triage dispatch command failed with exit {completed.returncode}")
        return DispatchResult(
            issue_number=request.issue_number,
            trigger_fingerprint=request.trigger_fingerprint,
            command=command,
        )


def triage_trigger_fingerprint(
    *,
    issue_number: int,
    material_message_ids: tuple[str, ...],
    asset_urls: tuple[str, ...],
) -> str:
    payload = {
        "issue_number": issue_number,
        "material_message_ids": sorted(material_message_ids),
        "asset_urls": sorted(asset_urls),
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _merge_unique(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for item in (*first, *second):
        if item and item not in values:
            values.append(item)
    return tuple(values)


def _format_command_part(part: str, request: TriageRequest) -> str:
    return part.format(
        issue_number=request.issue_number,
        trigger_fingerprint=request.trigger_fingerprint,
        reason=",".join(request.reasons),
    )
