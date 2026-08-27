"""Watermark decode result types and error codes.

This module deliberately imports nothing from the rest of the watermark
package (and no ``cryptography``), so the resource pipeline in
``bugpatrol.resources`` can type against it without dragging in crypto at
import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# A successful RS+JSON decode is deterministic proof, not a model guess.
FOUND_CONFIDENCE = 1.0

# Guard against a corrupted/oversized payload ballooning memory: the byte
# carriers (trailer / PNG text chunk) can carry arbitrary base64, so cap them.
MAX_ENVELOPE_BYTES = 512 * 1024

# Stable machine-readable error codes surfaced by the CLI and the pipeline.
ERROR_NOT_FOUND = "watermark_not_found"
ERROR_BAD_ENVELOPE = "watermark_invalid_envelope"

# Payload fields required on every decoded payload, mirroring the app's
# plaintext DiagnosticWatermarkPayload contract (v2). `uid` is dev-only and
# optional: prod builds omit it entirely.
PAYLOAD_REQUIRED_FIELDS = (
    "schemaVersion",
    "appVersion",
    "buildVersion",
    "buildTime",
    "modelName",
    "osName",
    "osVersion",
    "capturedAt",
)
PAYLOAD_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class WatermarkDecodeResult:
    """Outcome of decoding a watermark from one image."""

    found: bool
    confidence: float
    payload: dict[str, object] | None = None
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        if not self.found:
            return {
                "found": False,
                "confidence": 0,
                "error": self.error or ERROR_NOT_FOUND,
            }
        return {
            "found": True,
            "confidence": self.confidence,
            "payload": self.payload or {},
        }


class WatermarkDecoder(Protocol):
    """Deterministic decoder the resource pipeline can inject.

    Implementations must be pure over the input bytes: no prompt-based
    recognition, no network, no nondeterminism.
    """

    def decode(self, data: bytes) -> WatermarkDecodeResult:
        """Return a decode result for raw image bytes."""
