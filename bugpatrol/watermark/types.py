"""Watermark decode result types and error codes.

This module deliberately imports nothing from the rest of the watermark
package (and no ``cryptography``), so the resource pipeline in
``bugpatrol.resources`` can type against it without dragging in crypto at
import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Environment / secret names. The private key is NEVER committed anywhere; it is
# read from the environment or a secret store only. Rotation is handled by
# keying decryption on the envelope's ``keyId``.
ENV_PRIVATE_KEY = "FIVED_WATERMARK_PRIVATE_KEY_PEM"
ENV_KEYS_JSON = "FIVED_WATERMARK_KEYS_JSON"

# Matches the app's DIAGNOSTIC_WATERMARK_KEY_ID (app/lib/dev/diagnosticsClipboard.ts).
DEFAULT_KEY_ID = "diagnostic-watermark-v1"

# A successful authenticated decrypt is deterministic proof, not a model guess.
FOUND_CONFIDENCE = 1.0

# Guard against a corrupted/oversized payload ballooning memory: the envelope
# carries an AES-GCM ciphertext, which can legitimately be tens of KB, but never
# this. Anything bigger is a scan artifact, not a watermark. Shared by the
# byte-carrier extractor and the QR leg (which import from types to stay acyclic).
MAX_ENVELOPE_BYTES = 512 * 1024

# Stable machine-readable error codes surfaced by the CLI and the pipeline.
ERROR_NOT_FOUND = "watermark_not_found"
ERROR_KEY_MISSING = "watermark_private_key_missing"
ERROR_KEY_UNKNOWN = "watermark_key_not_found"
ERROR_DECRYPT = "watermark_decrypt_failed"
ERROR_BAD_ENVELOPE = "watermark_invalid_envelope"
ERROR_BAD_PAYLOAD = "watermark_invalid_payload"

# Payload fields required on every decoded payload, mirroring the app's
# DiagnosticWatermarkCorePayload contract.
PAYLOAD_REQUIRED_FIELDS = (
    "schemaVersion",
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


@dataclass(frozen=True)
class WatermarkDecodeResult:
    """Outcome of decoding a watermark from one image."""

    found: bool
    confidence: float
    key_id: str = ""
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
            "keyId": self.key_id,
            "payload": self.payload or {},
        }


class WatermarkDecoder(Protocol):
    """Deterministic decoder the resource pipeline can inject.

    Implementations must be pure over the input bytes: no prompt-based
    recognition, no network, no nondeterminism.
    """

    def decode(self, data: bytes) -> WatermarkDecodeResult:
        """Return a decode result for raw image bytes."""
