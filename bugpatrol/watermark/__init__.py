"""Deterministic plaintext-watermark decode for bug screenshots.

Decode the Five Degrees app's diagnostic watermark from a screenshot and
report structured metadata into the bug triage pipeline — all without any
prompt-based recognition.

The payload is PLAINTEXT (no encryption): the app embeds the compact JSON
directly into a spread-spectrum pixel carrier. The relay watcher extracts it
into the issue body; the triage runner parses it. There are no keys, no GH
secrets, no decryption step.

Pipeline ownership split (each leg is a separate module):

- ``extractor``  — image bytes -> plaintext payload JSON bytes
- ``reporter``   — payload JSON -> triage context
- CLI            — ``bugpatrol watermark decode --image <path> [--json]``

See docs/WATERMARK.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from bugpatrol.watermark.extractor import (
    CARRIER_END,
    CARRIER_START,
    PNG_WATERMARK_KEYWORD,
    WatermarkInvalidEnvelope,
    embed_payload_png_text,
    embed_payload_trailer,
    embed_screenshot_payload,
    extract_plaintext_payload,
)
from bugpatrol.watermark.reporter import (
    NO_WATERMARK_NOTE,
    payload_to_compact_json,
    render_payload_summary,
    watermark_failure_note,
)
from bugpatrol.watermark.types import (
    ERROR_BAD_ENVELOPE,
    ERROR_NOT_FOUND,
    FOUND_CONFIDENCE,
    MAX_ENVELOPE_BYTES,
    PAYLOAD_REQUIRED_FIELDS,
    WatermarkDecoder,
    WatermarkDecodeResult,
)

__all__ = [
    "CARRIER_END",
    "CARRIER_START",
    "ERROR_BAD_ENVELOPE",
    "ERROR_NOT_FOUND",
    "FOUND_CONFIDENCE",
    "MAX_ENVELOPE_BYTES",
    "NO_WATERMARK_NOTE",
    "PAYLOAD_REQUIRED_FIELDS",
    "PNG_WATERMARK_KEYWORD",
    "WatermarkDecodeResult",
    "WatermarkDecoder",
    "WatermarkInvalidEnvelope",
    "WatermarkResourceDecoder",
    "decode_image",
    "embed_payload_png_text",
    "embed_payload_trailer",
    "embed_screenshot_payload",
    "extract_plaintext_payload",
    "payload_to_compact_json",
    "render_payload_summary",
    "watermark_failure_note",
]


class WatermarkResourceDecoder:
    """Adapter for the resource pipeline: decodes raw image bytes."""

    def decode(self, data: bytes) -> WatermarkDecodeResult:
        return decode_image(data)


def decode_image(data_or_path: Path | bytes) -> WatermarkDecodeResult:
    """Decode a watermark from image bytes or a file path.

    Returns a ``WatermarkDecodeResult``; never raises for per-image outcomes.
    """
    data = data_or_path.read_bytes() if isinstance(data_or_path, Path) else data_or_path
    try:
        payload_bytes = extract_plaintext_payload(data)
    except WatermarkInvalidEnvelope:
        return WatermarkDecodeResult(found=False, confidence=0, error=ERROR_BAD_ENVELOPE)
    if payload_bytes is None:
        return WatermarkDecodeResult(found=False, confidence=0, error=ERROR_NOT_FOUND)
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return WatermarkDecodeResult(found=False, confidence=0, error=ERROR_BAD_ENVELOPE)
    if not isinstance(payload, dict):
        return WatermarkDecodeResult(found=False, confidence=0, error=ERROR_BAD_ENVELOPE)
    return WatermarkDecodeResult(found=True, confidence=FOUND_CONFIDENCE, payload=payload)
