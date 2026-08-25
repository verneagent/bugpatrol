"""Turn a decoded watermark payload into triage context and CLI output.

This is the reporter leg of the decode pipeline: ``decoded payload -> triage
context``. It is pure formatting — no crypto, no I/O — so it is trivially
deterministic and testable.
"""

from __future__ import annotations

import json

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
