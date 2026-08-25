"""QR / Data Matrix carrier leg: scan a barcode out of image bytes.

The app renders the encrypted envelope as a QR badge (see fived
``app/components/dev/DiagnosticScreenshotWatermark.tsx``). BugPatrol scans the
image with zxing-cpp and returns the envelope JSON inside the barcode.

zxing-cpp and numpy are imported lazily so the watermark import path stays
light when the QR carrier is never used (the byte carriers do not need them).
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from typing import Any

from PIL import Image, UnidentifiedImageError

from bugpatrol.watermark.types import MAX_ENVELOPE_BYTES

# Barcode formats we accept as watermark carriers (zxingcpp format names).
_SUPPORTED_FORMATS = {"QRCode", "DataMatrix"}


@dataclass(frozen=True)
class _BarcodeHit:
    format: str
    raw: bytes
    distance: float


def extract_qr_envelope_bytes(data: bytes) -> bytes | None:
    """Return the compact envelope JSON bytes scanned from a QR/Data Matrix.

    Returns ``None`` when the image carries no decodable barcode whose content
    is an envelope JSON object (including when the barcode scanner is
    unavailable). A barcode that IS present but does not parse as envelope JSON
    is treated as an unrelated on-screen QR (e.g. a share card), not as a
    damaged watermark — so it yields ``None`` rather than a hard failure.

    Selection is content-based: any barcode whose content parses as envelope
    JSON qualifies, so an unrelated on-screen QR does not shadow the
    watermark. When several qualify, the one nearest the top-left corner wins
    — that is where the app renders the badge.
    """
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        return None
    valid: list[tuple[float, bytes]] = []
    for hit in _scan_barcodes(image):
        if hit.format not in _SUPPORTED_FORMATS:
            continue
        if not hit.raw:
            continue
        envelope_bytes = _barcode_content_to_envelope(hit.raw)
        if envelope_bytes is not None:
            valid.append((hit.distance, envelope_bytes))
    if not valid:
        return None
    valid.sort(key=lambda item: item[0])
    return valid[0][1]


def _scan_barcodes(image: Image.Image) -> list[_BarcodeHit]:
    try:
        import zxingcpp
    except ImportError:
        return []
    hits: list[_BarcodeHit] = []
    found: list[Any] = []
    try:
        found = zxingcpp.read_barcodes(image)
    except (ValueError, TypeError, RuntimeError):
        found = []
    for barcode in found:
        raw = getattr(barcode, "bytes", None)
        if raw is None:
            text = getattr(barcode, "text", "")
            raw = text.encode("utf-8") if text else b""
        hits.append(
            _BarcodeHit(
                format=str(getattr(getattr(barcode, "format", None), "name", "")),
                raw=bytes(raw),
                distance=_barcode_distance(getattr(barcode, "position", None)),
            )
        )
    return hits


def _barcode_content_to_envelope(raw: bytes) -> bytes | None:
    """Turn a barcode's raw content into compact envelope JSON bytes.

    The app writes compact JSON directly. A base64-wrapped variant is accepted
    for robustness (mirrors the legacy byte-carrier format). Returns ``None``
    when the content is not an envelope JSON object (i.e. it is some other QR).
    """
    parsed = _parse_json_object(raw)
    if parsed is not None:
        return _compact_json_bytes(parsed)
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError:
        return None
    parsed = _parse_json_object(decoded)
    if parsed is not None:
        return _compact_json_bytes(parsed)
    return None


def _parse_json_object(raw: bytes) -> dict[str, object] | None:
    if not raw or len(raw) > MAX_ENVELOPE_BYTES:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _compact_json_bytes(obj: dict[str, object]) -> bytes:
    """Mirror of ``extractor._compact_json_bytes`` (kept local to stay acyclic)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _barcode_distance(position: object) -> float:
    """Euclidean distance of a barcode's top-left corner from the image origin.

    The app renders the watermark badge near the top-left corner, so the
    nearest qualifying barcode is the watermark when several collide.
    """
    if position is None:
        return float("inf")
    corner = getattr(position, "top_left", None)
    if corner is not None:
        try:
            return float(corner.x) ** 2 + float(corner.y) ** 2
        except (AttributeError, TypeError):
            return float("inf")
    # Only the zxingcpp Position shape is known; anything else is unscored.
    return float("inf")
