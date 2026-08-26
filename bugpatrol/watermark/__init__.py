"""Deterministic invisible-watermark decode for bug screenshots.

Decode the Five Degrees app's diagnostic watermark from a screenshot, decrypt
the embedded payload with the private key from the environment, and report
structured metadata into the bug triage pipeline — all without any prompt-based
recognition.

Pipeline ownership split (each leg is a separate module):

- ``extractor``  — image bytes -> encrypted envelope
- ``decryptor``  — encrypted envelope -> JSON payload
- ``reporter``   — JSON payload -> triage context
- CLI            — ``bugpatrol watermark decode --image <path> [--json]``

Secrets: the private key is NEVER committed. Decryption reads it from
``FIVED_WATERMARK_PRIVATE_KEY_PEM`` (GitHub Actions repository secret, or the
same-named local dev variable); rotation adds ``FIVED_WATERMARK_KEYS_JSON``
mapping ``keyId`` to PEM. See docs/WATERMARK.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from bugpatrol.watermark.decryptor import (
    WatermarkBadPayload,
    WatermarkDecryptError,
    decrypt_envelope,
    validate_payload,
)
from bugpatrol.watermark.envelope import (
    ENVELOPE_ALG,
    ENVELOPE_VERSION,
    WatermarkBadEnvelope,
    build_envelope,
    parse_envelope,
)
from bugpatrol.watermark.extractor import (
    CARRIER_END,
    CARRIER_START,
    PNG_WATERMARK_KEYWORD,
    WatermarkInvalidEnvelope,
    embed_envelope_trailer,
    embed_png_text_envelope,
    embed_screenshot_pixel_envelope,
    extract_envelope_bytes,
    extract_envelope_candidates,
    iter_envelope_candidates,
)
from bugpatrol.watermark.keys import (
    WatermarkBadKeyConfig,
    WatermarkKeyError,
    WatermarkKeyNotFound,
    WatermarkKeyStore,
)
from bugpatrol.watermark.reporter import (
    NO_WATERMARK_NOTE,
    candidates_to_compact_json,
    payload_to_compact_json,
    render_payload_summary,
    watermark_failure_note,
)
from bugpatrol.watermark.types import (
    DEFAULT_KEY_ID,
    ENV_KEYS_JSON,
    ENV_PRIVATE_KEY,
    ERROR_BAD_ENVELOPE,
    ERROR_BAD_PAYLOAD,
    ERROR_DECRYPT,
    ERROR_KEY_MISSING,
    ERROR_KEY_UNKNOWN,
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
    "DEFAULT_KEY_ID",
    "ENVELOPE_ALG",
    "ENVELOPE_VERSION",
    "ENV_KEYS_JSON",
    "ENV_PRIVATE_KEY",
    "ERROR_BAD_ENVELOPE",
    "ERROR_BAD_PAYLOAD",
    "ERROR_DECRYPT",
    "ERROR_KEY_MISSING",
    "ERROR_KEY_UNKNOWN",
    "ERROR_NOT_FOUND",
    "FOUND_CONFIDENCE",
    "MAX_ENVELOPE_BYTES",
    "NO_WATERMARK_NOTE",
    "PAYLOAD_REQUIRED_FIELDS",
    "PNG_WATERMARK_KEYWORD",
    "WatermarkBadEnvelope",
    "WatermarkBadKeyConfig",
    "WatermarkBadPayload",
    "WatermarkDecodeResult",
    "WatermarkDecoder",
    "WatermarkDecryptError",
    "WatermarkInvalidEnvelope",
    "WatermarkKeyError",
    "WatermarkKeyNotFound",
    "WatermarkKeyStore",
    "WatermarkResourceDecoder",
    "build_envelope",
    "candidates_to_compact_json",
    "decode_image",
    "decrypt_envelope",
    "embed_envelope_trailer",
    "embed_png_text_envelope",
    "embed_screenshot_pixel_envelope",
    "extract_envelope_bytes",
    "extract_envelope_candidates",
    "iter_envelope_candidates",
    "parse_envelope",
    "payload_to_compact_json",
    "render_payload_summary",
    "validate_payload",
    "watermark_failure_note",
]


class WatermarkResourceDecoder:
    """Adapter for the resource pipeline: decodes raw image bytes.

    Reads keys from the environment on each call (unless an explicit key store
    is injected), so a fresh key in the environment is picked up without a
    process restart. Inert when no key is configured: returns a
    ``watermark_private_key_missing`` result.
    """

    def __init__(self, key_store: WatermarkKeyStore | None = None) -> None:
        self._key_store = key_store

    def decode(self, data: bytes) -> WatermarkDecodeResult:
        return decode_image(data, key_store=self._key_store)


def decode_image(
    data_or_path: Path | bytes,
    *,
    key_store: WatermarkKeyStore | None = None,
) -> WatermarkDecodeResult:
    """Decode a watermark from image bytes or a file path.

    Returns a ``WatermarkDecodeResult``; never raises for per-image outcomes.
    Configuration errors (malformed key env) still raise so they surface.
    """
    data = data_or_path.read_bytes() if isinstance(data_or_path, Path) else data_or_path
    store = key_store if key_store is not None else WatermarkKeyStore.from_env()
    if not store.has_keys():
        return WatermarkDecodeResult(found=False, confidence=0, error=ERROR_KEY_MISSING)
    # A pixel read at a wrong ±1px offset can look like a valid envelope but
    # carry corrupted ciphertext. GCM authentication is the ground truth, so
    # pull candidates lazily (best-first) and stop at the first that decrypts:
    # the clean read (e.g. the other corner) wins over a look-alike that fails
    # to decrypt, and a clean screenshot costs one read, not the full
    # scale/corner/offset probe space.
    last_error = ERROR_NOT_FOUND
    # The generator can raise WatermarkInvalidEnvelope lazily while iterating
    # (e.g. a carrier start marker with no end), so the try must span the loop.
    try:
        candidates = iter_envelope_candidates(data)
        for envelope_bytes in candidates:
            try:
                envelope = json.loads(envelope_bytes.decode("utf-8"))
                payload = decrypt_envelope(envelope, store)
            except WatermarkBadEnvelope:
                last_error = ERROR_BAD_ENVELOPE
                continue
            except WatermarkKeyNotFound:
                return WatermarkDecodeResult(found=False, confidence=0, error=ERROR_KEY_UNKNOWN)
            except WatermarkDecryptError:
                last_error = ERROR_DECRYPT
                continue
            except WatermarkBadPayload:
                last_error = ERROR_BAD_PAYLOAD
                continue
            return WatermarkDecodeResult(
                found=True,
                confidence=FOUND_CONFIDENCE,
                key_id=str(payload.get("keyId") or ""),
                payload=payload,
            )
    except WatermarkInvalidEnvelope:
        return WatermarkDecodeResult(found=False, confidence=0, error=ERROR_BAD_ENVELOPE)
    return WatermarkDecodeResult(found=False, confidence=0, error=last_error)
