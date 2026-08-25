"""Turn a decoded watermark payload into triage context and CLI output.

This is the reporter leg of the decode pipeline: ``decoded payload -> triage
context``. It is pure formatting — no crypto, no I/O — so it is trivially
deterministic and testable.
"""

from __future__ import annotations

import json

# Issue-body value for an attachment that was scanned for a watermark and
# carried none. Rendered verbatim as `- watermark: 未找到水印` so the triage
# agent sees an explicit "checked, absent" status instead of no line at all.
NO_WATERMARK_NOTE = "未找到水印"


def watermark_failure_note(error: str) -> str:
    """Issue-body note for a watermark decode that failed (e.g. corrupt envelope)."""
    return f"水印解码失败 ({error})"


_ORDERED_FIELDS = (
    "keyId",
    "watermarkId",
    "uid",
    "pathname",
    "platform",
    "appVersion",
    "buildVersion",
    "buildInfo",
    "gitCommit",
    "buildTime",
    "modelName",
    "osName",
    "osVersion",
    "capturedAt",
)


def payload_to_compact_json(payload: dict[str, object]) -> str:
    """Single-line JSON, stable ordering, for ``Attachment.watermark`` storage."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def candidates_to_compact_json(candidates: list[bytes]) -> str:
    """Single-line JSON array of envelope dicts, for ``Attachment.watermark``.

    The relay watcher has no private key, so it stores every structurally-valid
    envelope extracted from the raw bytes **unverified**. The triage runner
    later decrypts each candidate with the GH Actions key and GCM auth picks the
    clean one — see ``triage_context.resolve_media_watermarks``.
    """
    return json.dumps(
        [json.loads(envelope_bytes.decode("utf-8")) for envelope_bytes in candidates],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def render_payload_summary(payload: dict[str, object]) -> str:
    """A single-line human/agent-readable summary of a decoded payload.

    Every core field is included as ``key=value`` so the triage agent can read
    device/build/identity context off the screenshot without parsing JSON.
    """
    parts: list[str] = []
    for field in _ORDERED_FIELDS:
        value = payload.get(field)
        if value is None or value == "":
            continue
        parts.append(f"{field}={value}")
    return "[Watermark] " + " ".join(parts)
