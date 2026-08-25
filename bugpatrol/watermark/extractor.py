"""Extract an encrypted watermark envelope from image bytes.

The app embeds the encrypted envelope (a base64-encoded JSON object) into a
screenshot using a deterministic carrier. Two carriers are supported, both of
which survive normal image capture:

1. **Trailer carrier** (canonical): the bytes ``BUGPATROL_WM1:<b64>:BUGPATROL_WM1``
   are appended after the image's natural end. PNG viewers stop at IEND and JPEG
   decoders stop at EOI, so the trailer is invisible but present in the file.
2. **PNG text-chunk carrier**: a PNG ``tEXt`` chunk with keyword
   ``bugpatrol.watermark`` whose value is the base64 envelope.

This module is deterministic — it only locates and decodes the carrier. It does
not decrypt (see ``decryptor``) and never runs a model.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib

CARRIER_START = b"BUGPATROL_WM1:"
CARRIER_END = b":BUGPATROL_WM1"
PNG_WATERMARK_KEYWORD = b"bugpatrol.watermark"
# Guard against a corrupted/oversized payload ballooning memory: the envelope
# carries an AES-GCM ciphertext, which can legitimately be tens of KB, but never
# this. Anything bigger is a scan artifact, not a watermark.
MAX_ENVELOPE_BYTES = 512 * 1024

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class WatermarkInvalidEnvelope(Exception):
    """A watermark carrier was found but its contents are corrupt."""


def extract_envelope_bytes(data: bytes) -> bytes | None:
    """Return the compact JSON bytes of the embedded envelope, or None.

    Raises ``WatermarkInvalidEnvelope`` when a carrier is present but the
    payload inside it is not a valid base64-encoded JSON envelope.
    """
    from_marker = _extract_from_markers(data)
    if from_marker is not None:
        return from_marker
    if data.startswith(PNG_SIGNATURE):
        from_chunk = _extract_from_png_text_chunk(data)
        if from_chunk is not None:
            return from_chunk
    return None


def embed_envelope_trailer(image_bytes: bytes, envelope: dict[str, object]) -> bytes:
    """Append the trailer carrier to image bytes (fixture/reference embedding)."""
    encoded = base64.b64encode(_compact_json_bytes(envelope))
    return image_bytes + CARRIER_START + encoded + CARRIER_END


def embed_png_text_envelope(png_bytes: bytes, envelope: dict[str, object]) -> bytes:
    """Insert a ``bugpatrol.watermark`` tEXt chunk before IEND (PNG only)."""
    if not png_bytes.startswith(PNG_SIGNATURE):
        raise ValueError("embed_png_text_envelope requires PNG input")
    encoded = base64.b64encode(_compact_json_bytes(envelope))
    text_chunk = _png_text_chunk(keyword=PNG_WATERMARK_KEYWORD, text=encoded)
    return _insert_chunk_before_iend(png_bytes, text_chunk)


def _extract_from_markers(data: bytes) -> bytes | None:
    start = data.find(CARRIER_START)
    if start < 0:
        return None
    payload_start = start + len(CARRIER_START)
    end = data.find(CARRIER_END, payload_start)
    if end < 0:
        raise WatermarkInvalidEnvelope("watermark carrier start found but no end marker")
    raw = data[payload_start:end]
    return _decode_envelope_raw(raw)


def _extract_from_png_text_chunk(data: bytes) -> bytes | None:
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        if length > MAX_ENVELOPE_BYTES:
            # PNG chunk length is a uint32; bail on a pathological header early.
            if chunk_type == b"IEND":
                break
            offset += 12 + length
            continue
        if chunk_type in (b"tEXt", b"iTXt"):
            text = _png_chunk_text(chunk_type, chunk_data)
            if text is not None:
                decoded = _decode_envelope_raw(text)
                if decoded is not None:
                    return decoded
        if chunk_type == b"IEND":
            break
        offset += 12 + length
    return None


def _png_chunk_text(chunk_type: bytes, chunk_data: bytes) -> bytes | None:
    if chunk_type == b"tEXt":
        keyword, sep, text = chunk_data.partition(b"\x00")
        if not sep:
            return None
        return text if keyword == PNG_WATERMARK_KEYWORD else None
    if chunk_type == b"iTXt":
        # iTXt: keyword \x00 compression-flag \x00 compression-method \x00
        # language-tag \x00 translated-keyword \x00 text
        parts = chunk_data.split(b"\x00", 5)
        if len(parts) < 6 or parts[0] != PNG_WATERMARK_KEYWORD:
            return None
        return parts[5]
    return None


def _decode_envelope_raw(raw: bytes) -> bytes | None:
    if not raw or len(raw) > MAX_ENVELOPE_BYTES:
        return None
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise WatermarkInvalidEnvelope("watermark payload is not valid base64") from exc
    try:
        parsed = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatermarkInvalidEnvelope("watermark payload is not valid envelope JSON") from exc
    if not isinstance(parsed, dict):
        raise WatermarkInvalidEnvelope("watermark envelope must be a JSON object")
    return _compact_json_bytes(parsed)


def _compact_json_bytes(obj: dict[str, object]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _png_text_chunk(*, keyword: bytes, text: bytes) -> bytes:
    chunk_data = keyword + b"\x00" + text
    chunk_type = b"tEXt"
    return _png_chunk(chunk_type, chunk_data)


def _png_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
    length = struct.pack(">I", len(chunk_data))
    crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
    return length + chunk_type + chunk_data + struct.pack(">I", crc)


def _insert_chunk_before_iend(png_bytes: bytes, new_chunk: bytes) -> bytes:
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(png_bytes):
        length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
        chunk_type = png_bytes[offset + 4 : offset + 8]
        if chunk_type == b"IEND":
            return png_bytes[:offset] + new_chunk + png_bytes[offset:]
        offset += 12 + length
    raise ValueError("PNG has no IEND chunk")
