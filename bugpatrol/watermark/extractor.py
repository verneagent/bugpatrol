"""Extract an encrypted watermark envelope from image bytes.

The app embeds the encrypted envelope JSON into a screenshot using a
deterministic pixel carrier. Legacy byte carriers are still supported for
existing fixtures and reference tools:

1. **Screenshot pixel carrier** (canonical): the app renders a root overlay
   into every native screenshot. Adjacent light/dark cells encode the encrypted
   envelope while mostly cancelling out visually against the page.
2. **Trailer carrier** (legacy/reference): the bytes
   ``BUGPATROL_WM1:<b64>:BUGPATROL_WM1`` are appended after the image's natural
   end. PNG viewers stop at IEND and JPEG decoders stop at EOI, so the trailer is
   invisible but present in the file.
3. **PNG text-chunk carrier** (legacy/reference): a PNG ``tEXt`` chunk with keyword
   ``bugpatrol.watermark`` whose value is the base64 envelope.

This module is deterministic — it only locates and decodes the carrier. It does
not decrypt (see ``decryptor``) and never runs a model.
"""

from __future__ import annotations

import base64
import io
import json
import math
import struct
import zlib
from typing import cast

from PIL import Image, ImageDraw, UnidentifiedImageError

from bugpatrol.watermark.qr import extract_qr_envelope_bytes
from bugpatrol.watermark.types import MAX_ENVELOPE_BYTES

CARRIER_START = b"BUGPATROL_WM1:"


class WatermarkInvalidEnvelope(Exception):
    """A watermark carrier was found but its contents are corrupt."""
CARRIER_END = b":BUGPATROL_WM1"
PNG_WATERMARK_KEYWORD = b"bugpatrol.watermark"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PIXEL_BIT_COLUMNS = 128
PIXEL_BIT_ROWS = 256
PIXEL_LENGTH_BITS = 16
PIXEL_WIDTH_MODULES = PIXEL_BIT_COLUMNS * 2
PIXEL_HEIGHT_MODULES = PIXEL_BIT_ROWS
PIXEL_OFFSET_MODULES = 6
PIXEL_MAX_ENVELOPE_BYTES = (PIXEL_BIT_COLUMNS * PIXEL_BIT_ROWS - PIXEL_LENGTH_BITS) // 8
PIXEL_SCALE_CANDIDATES = tuple(1.0 + index * 0.125 for index in range(25))


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
    from_pixels = _extract_from_screenshot_pixels(data)
    if from_pixels is not None:
        return from_pixels
    # QR/Data Matrix is the last resort: a visual barcode, scanned out of the
    # image rather than parsed out of the byte layout. Keep it last so byte
    # carriers stay canonical (they never degrade under re-encoding).
    from_qr = extract_qr_envelope_bytes(data)
    if from_qr is not None:
        return from_qr
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


def embed_screenshot_pixel_envelope(
    image_bytes: bytes,
    envelope: dict[str, object],
    *,
    scale: float = 2,
    corner: str = "top_left",
    shift: tuple[int, int] = (0, 0),
) -> bytes:
    """Overlay the screenshot-time paired-cell pixel carrier on a PNG fixture.

    This mirrors the app's root overlay: each bit is encoded as adjacent
    light/dark cells so local background cancels out during extraction.
    ``scale=3`` reproduces the app's fixed-3px whole-pixel geometry; ``shift``
    simulates the layout rounding that the extractor's ±1px probe absorbs.
    """
    envelope_bytes = _compact_json_bytes(envelope)
    if len(envelope_bytes) > PIXEL_MAX_ENVELOPE_BYTES:
        raise ValueError("envelope is too large for screenshot pixel carrier")
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    origin_x, origin_y = _pixel_origin(image.width, image.height, scale, corner)
    origin_x += shift[0]
    origin_y += shift[1]
    draw = ImageDraw.Draw(image, "RGBA")
    for bit_index, bit in enumerate(_pixel_bits(envelope_bytes)):
        x = origin_x + (bit_index % PIXEL_BIT_COLUMNS) * 2 * scale
        y = origin_y + (bit_index // PIXEL_BIT_COLUMNS) * scale
        dark_x = x if bit == 1 else x + scale
        light_x = x + scale if bit == 1 else x
        draw.rectangle((light_x, y, light_x + scale - 1, y + scale - 1), fill=(255, 255, 255, 13))
        draw.rectangle((dark_x, y, dark_x + scale - 1, y + scale - 1), fill=(0, 0, 0, 13))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


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


def _extract_from_screenshot_pixels(data: bytes) -> bytes | None:
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        return None
    for scale in PIXEL_SCALE_CANDIDATES:
        if image.width < PIXEL_WIDTH_MODULES * scale or image.height < PIXEL_HEIGHT_MODULES * scale:
            continue
        for corner in ("top_left", "bottom_right"):
            decoded = _read_pixel_carrier_at(image, scale=scale, corner=corner)
            if decoded is not None:
                return decoded
    return None


def _read_pixel_carrier_at(image: Image.Image, *, scale: float, corner: str) -> bytes | None:
    """Return the compact envelope JSON at ``corner``, or None.

    The app renders cells at whole physical px, but a device's layout rounding
    can shift the grid by a pixel or two. Probe a small neighborhood of
    offsets and return the first one whose header + payload decode to valid
    envelope JSON, so sub-pixel misalignment never silently loses the carrier.
    """
    origin_x, origin_y = _pixel_origin(image.width, image.height, scale, corner)
    for offset_x, offset_y in _carrier_offset_candidates():
        raw = _read_carrier_bytes(
            image,
            origin_x=origin_x + offset_x,
            origin_y=origin_y + offset_y,
            scale=scale,
        )
        if raw is None:
            continue
        try:
            return _decode_pixel_envelope(raw)
        except WatermarkInvalidEnvelope:
            continue
    return None


def _carrier_offset_candidates() -> tuple[tuple[int, int], ...]:
    """Offsets to probe, nearest first (0,0 is the app's nominal grid origin)."""
    candidates = [(0, 0)]
    candidates.extend(
        (offset_x, offset_y)
        for offset_y in (-1, 0, 1)
        for offset_x in (-1, 0, 1)
        if (offset_x, offset_y) != (0, 0)
    )
    candidates.sort(key=lambda item: abs(item[0]) + abs(item[1]))
    return tuple(candidates)


def _read_carrier_bytes(image: Image.Image, *, origin_x: int, origin_y: int, scale: float) -> bytes | None:
    length = 0
    for bit_index in range(PIXEL_LENGTH_BITS):
        bit = _read_pixel_bit(image, origin_x=origin_x, origin_y=origin_y, scale=scale, bit_index=bit_index)
        if bit is None:
            return None
        length = (length << 1) | bit
    if length <= 0 or length > PIXEL_MAX_ENVELOPE_BYTES:
        return None
    values = bytearray()
    for byte_index in range(length):
        value = 0
        for bit_in_byte in range(8):
            bit_index = PIXEL_LENGTH_BITS + byte_index * 8 + bit_in_byte
            bit = _read_pixel_bit(image, origin_x=origin_x, origin_y=origin_y, scale=scale, bit_index=bit_index)
            if bit is None:
                return None
            value = (value << 1) | bit
        values.append(value)
    return bytes(values)


def _read_pixel_bit(
    image: Image.Image,
    *,
    origin_x: int,
    origin_y: int,
    scale: float,
    bit_index: int,
) -> int | None:
    x = origin_x + (bit_index % PIXEL_BIT_COLUMNS) * 2 * scale
    y = origin_y + (bit_index // PIXEL_BIT_COLUMNS) * scale
    left = _sample_cell_luma(image, x, y, scale)
    right = _sample_cell_luma(image, x + scale, y, scale)
    if left is None or right is None:
        return None
    delta = left - right
    if abs(delta) < 0.25:
        return None
    return 1 if delta < 0 else 0


def _sample_cell_luma(image: Image.Image, x: float, y: float, scale: float) -> float | None:
    start_x = max(0, math.floor(x))
    end_x = min(image.width - 1, math.ceil(x + scale) - 1)
    start_y = max(0, math.floor(y))
    end_y = min(image.height - 1, math.ceil(y + scale) - 1)
    if start_x > end_x or start_y > end_y:
        return None
    total = 0.0
    count = 0
    for py in range(start_y, end_y + 1):
        for px in range(start_x, end_x + 1):
            value = _sample_luma(image, px, py)
            if value is None:
                continue
            total += value
            count += 1
    return total / count if count > 0 else None


def _sample_luma(image: Image.Image, x: float, y: float) -> float | None:
    px = round(x)
    py = round(y)
    if px < 0 or py < 0 or px >= image.width or py >= image.height:
        return None
    r, g, b = cast(tuple[int, int, int], image.getpixel((px, py)))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _decode_pixel_envelope(raw: bytes) -> bytes:
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise WatermarkInvalidEnvelope("watermark payload is too large")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatermarkInvalidEnvelope("watermark pixel payload is not valid envelope JSON") from exc
    if not isinstance(parsed, dict):
        raise WatermarkInvalidEnvelope("watermark pixel envelope must be a JSON object")
    return _compact_json_bytes(parsed)


def _pixel_origin(width: int, height: int, scale: float, corner: str) -> tuple[int, int]:
    offset = round(PIXEL_OFFSET_MODULES * scale)
    carrier_width = round(PIXEL_WIDTH_MODULES * scale)
    carrier_height = round(PIXEL_HEIGHT_MODULES * scale)
    if corner == "bottom_right":
        return width - carrier_width - offset, height - carrier_height - offset
    return offset, offset


def _pixel_bits(envelope_bytes: bytes) -> tuple[int, ...]:
    bits: list[int] = []
    for shift in range(PIXEL_LENGTH_BITS - 1, -1, -1):
        bits.append((len(envelope_bytes) >> shift) & 1)
    for byte in envelope_bytes:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
    return tuple(bits)


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
