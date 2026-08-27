"""Extract a plaintext diagnostic watermark payload from image bytes.

The app embeds the plaintext payload JSON into a screenshot using a
deterministic spread-spectrum pixel carrier. Legacy byte carriers are still
supported for existing fixtures and reference tools:

1. **Spread-spectrum pixel carrier** (canonical): the app renders a full-screen
   root overlay into every native screenshot. Each bit is carried by 4
   horizontal adjacent chip pairs (spread-spectrum LCG coordinates, 18% alpha).
   RS(255,135)×2 error correction survives the real production channel.
2. **Trailer carrier** (legacy/reference): the bytes
   ``BUGPATROL_WM1:<b64>:BUGPATROL_WM1`` are appended after the image's natural
   end. PNG viewers stop at IEND and JPEG decoders stop at EOI, so the trailer is
   invisible but present in the file.
3. **PNG text-chunk carrier** (legacy/reference): a PNG ``tEXt`` chunk with keyword
   ``bugpatrol.watermark`` whose value is the base64 payload.

This module is deterministic — it only locates and decodes the carrier. There is
no decryption (the payload is plaintext) and it never runs a model.
"""

from __future__ import annotations

import base64
import io
import json
import struct
import typing
import zlib

from PIL import Image, ImageDraw, UnidentifiedImageError

from bugpatrol.watermark.rs256 import rs_correct_msg, rs_encode_msg
from bugpatrol.watermark.types import MAX_ENVELOPE_BYTES

CARRIER_START = b"BUGPATROL_WM1:"
CARRIER_END = b":BUGPATROL_WM1"
PNG_WATERMARK_KEYWORD = b"bugpatrol.watermark"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class WatermarkInvalidEnvelope(Exception):
    """A watermark carrier was found but its contents are corrupt."""


# --- Spread-spectrum paired-cell carrier (RS(255,135)×2 + LCG spread + H2 pairs)
#
# The carrier is rendered on a NOMINAL 1080×2340 canvas. The app draws a
# full-screen SVG with viewBox `0 0 1080 2340` (preserveAspectRatio meet); it
# scales to the native screen, and Lark downscales screenshots to 1080 wide.
# Those two scalings cancel exactly, so chips land back at their nominal
# coordinates in the final image — the extractor reads at nominal coords with
# NO knowledge of the native resolution and NO coordinate remapping.
#
# Each bit is carried by PAIR_COUNT=4 horizontal chip pairs at LCG coordinates.
# A pair is two 3×3 chips, centers 4px apart: bit=1 → left dark / right light,
# bit=0 → reversed. The luma delta of a pair is ~alpha*255 regardless of the
# background (alpha-invariant), so a busy UI cannot flip the bit's sign the way
# a single cell could. The extractor majority-votes the up-to-4 pair deltas.
#
# Error correction: payload = [magic 0x4D58][len 2B BE][payload] zero-padded to
# 2×135 bytes, each block RS(255,135)-encoded (nsym=120 parity → up to 60 byte
# errors/block corrected) → 510 encoded bytes → 4080 bits. Coverage (11.6%) is
# independent of RS parameters, so we take the maximum parity the payload fits.
#
# Constants MUST match the app's TypeScript builder
# (app/lib/dev/diagnosticScreenshotWatermarkPixels.ts) exactly.
NOMINAL_WIDTH = 1080
NOMINAL_HEIGHT = 2340
NOMINAL_MARGIN = 0.02
LCG_SEED = 0x5EEDCAFE
LCG_A = 1103515245
LCG_C = 12345
PAIR_COUNT = 4
PAIR_OFFSET = 2  # pa at (cx-2, cy), pb at (cx+2, cy); centers 4px apart.
RS_BLOCK_COUNT = 2
RS_NSYM = 120
RS_K = 255 - RS_NSYM  # 135
RS_ENCODED_BYTES = RS_BLOCK_COUNT * 255  # 510
RS_DATA_TOTAL = RS_BLOCK_COUNT * RS_K  # 270
WM_MAGIC = b"\x4d\x58"
WM_MAGIC_WORD = 0x4D58
# [magic 2B][len 2B] + payload, shared across the 2 RS data blocks.
WM_MAX_PAYLOAD_BYTES = RS_DATA_TOTAL - 2 - 2  # 266


# --- Portable LCG coordinate generator (must match the app's TypeScript).
def _math_mul(a: int, b: int) -> int:
    """32-bit multiply via 16-bit decomposition (equals JS Math.imul)."""
    a &= 0xFFFFFFFF
    b &= 0xFFFFFFFF
    ah = (a >> 16) & 0xFFFF
    al = a & 0xFFFF
    bh = (b >> 16) & 0xFFFF
    bl = b & 0xFFFF
    return ((al * bl) + (((ah * bl + al * bh) & 0xFFFF) << 16)) & 0xFFFFFFFF


def _lcg(seed: int) -> typing.Iterator[int]:
    state = seed & 0xFFFFFFFF
    while True:
        state = (_math_mul(LCG_A, state) + LCG_C) & 0xFFFFFFFF
        yield state


def gen_centers(n_chips: int, *, margin: float = NOMINAL_MARGIN) -> list[tuple[int, int]]:
    """LCG-jittered grid centers, Fisher-Yates shuffled, first ``n_chips`` kept.

    Consumes the LCG in the exact order the app does: row-major cell traversal
    (two draws per cell for the x/y jitter), then one draw per shuffle index.
    """
    rnd = _lcg(LCG_SEED)
    lx, hx = int(NOMINAL_WIDTH * margin), NOMINAL_WIDTH - int(NOMINAL_WIDTH * margin)
    ly, hy = int(NOMINAL_HEIGHT * margin), NOMINAL_HEIGHT - int(NOMINAL_HEIGHT * margin)
    g = int(((hx - lx) * (hy - ly) / n_chips) ** 0.5)
    g = max(g, 9)
    while True:
        cells = [
            (gx + next(rnd) % (g // 2 + 1) - g // 4, gy + next(rnd) % (g // 2 + 1) - g // 4)
            for gy in range(ly, hy, g)
            for gx in range(lx, hx, g)
        ]
        if len(cells) >= n_chips:
            break
        g -= 1
    for i in range(len(cells) - 1, 0, -1):
        j = next(rnd) % (i + 1)
        cells[i], cells[j] = cells[j], cells[i]
    return cells[:n_chips]


def extract_plaintext_payload(data: bytes) -> bytes | None:
    """Return the compact JSON bytes of the embedded plaintext payload, or None.

    Raises ``WatermarkInvalidEnvelope`` when a byte carrier is present but its
    contents are corrupt. The canonical pixel carrier returns ``None`` when the
    RS decode cannot recover a payload (deterministic — no valid-looking wrong
    reads survive RS + magic + JSON, unlike the old ciphertext candidates).
    """
    from_marker = _extract_from_markers(data)
    if from_marker is not None:
        return from_marker
    if data.startswith(PNG_SIGNATURE):
        from_chunk = _extract_from_png_text_chunk(data)
        if from_chunk is not None:
            return from_chunk
    return _extract_pixel_payload(data)


def embed_payload_trailer(image_bytes: bytes, payload: dict[str, object]) -> bytes:
    """Append the trailer carrier to image bytes (fixture/reference embedding)."""
    encoded = base64.b64encode(_compact_json_bytes(payload))
    return image_bytes + CARRIER_START + encoded + CARRIER_END


def embed_payload_png_text(png_bytes: bytes, payload: dict[str, object]) -> bytes:
    """Insert a ``bugpatrol.watermark`` tEXt chunk before IEND (PNG only)."""
    if not png_bytes.startswith(PNG_SIGNATURE):
        raise ValueError("embed_payload_png_text requires PNG input")
    encoded = base64.b64encode(_compact_json_bytes(payload))
    text_chunk = _png_text_chunk(keyword=PNG_WATERMARK_KEYWORD, text=encoded)
    return _insert_chunk_before_iend(png_bytes, text_chunk)


def embed_screenshot_payload(
    image_bytes: bytes,
    payload: dict[str, object],
    *,
    alpha: float = 0.18,
) -> bytes:
    """Overlay the spread-spectrum carrier on a PNG fixture (mirrors the app).

    Draws the same LCG H2-pair geometry the app renders, scaled from the
    nominal 1080×2340 canvas to the fixture's native size (crisp integer chip
    placement). ``alpha`` matches the app's 0.18 fill opacity.
    """
    payload_bytes = _compact_json_bytes(payload)
    if len(payload_bytes) > WM_MAX_PAYLOAD_BYTES:
        raise ValueError("payload is too large for screenshot pixel carrier")
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")
    bits = _payload_to_bits(payload_bytes)
    n_chips = len(bits) * PAIR_COUNT
    centers = gen_centers(n_chips)
    sx = image.width / NOMINAL_WIDTH
    sy = image.height / NOMINAL_HEIGHT
    dark_fill = (0, 0, 0, round(255 * alpha))
    light_fill = (255, 255, 255, round(255 * alpha))
    for index, (cx, cy) in enumerate(centers):
        bit = bits[index // PAIR_COUNT]
        pa_dark = bit == 1
        for (chip_x, chip_y), is_dark in (
            ((cx - PAIR_OFFSET, cy), pa_dark),
            ((cx + PAIR_OFFSET, cy), not pa_dark),
        ):
            x = int(round(chip_x * sx))
            y = int(round(chip_y * sy))
            fill = dark_fill if is_dark else light_fill
            draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=fill)
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
    return _decode_payload_raw(raw)


def _extract_from_png_text_chunk(data: bytes) -> bytes | None:
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        if length > MAX_ENVELOPE_BYTES:
            if chunk_type == b"IEND":
                break
            offset += 12 + length
            continue
        if chunk_type in (b"tEXt", b"iTXt"):
            text = _png_chunk_text(chunk_type, chunk_data)
            if text is not None:
                decoded = _decode_payload_raw(text)
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
        parts = chunk_data.split(b"\x00", 5)
        if len(parts) < 6 or parts[0] != PNG_WATERMARK_KEYWORD:
            return None
        return parts[5]
    return None


def _decode_payload_raw(raw: bytes) -> bytes | None:
    if not raw or len(raw) > MAX_ENVELOPE_BYTES:
        return None
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise WatermarkInvalidEnvelope("watermark payload is not valid base64") from exc
    try:
        parsed = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatermarkInvalidEnvelope("watermark payload is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise WatermarkInvalidEnvelope("watermark payload must be a JSON object")
    return _compact_json_bytes(parsed)


def _extract_pixel_payload(data: bytes) -> bytes | None:
    """RS-decode the spread-spectrum pixel carrier, or None if absent/garbage."""
    try:
        image = Image.open(io.BytesIO(data)).convert("L")
    except (UnidentifiedImageError, OSError):
        return None
    if image.width != NOMINAL_WIDTH:
        image = image.resize(
            (NOMINAL_WIDTH, round(image.height * NOMINAL_WIDTH / image.width)),
            Image.LANCZOS,
        )
    luma = image.tobytes()
    encoded = _read_pixel_stream(luma, image.width, image.height)
    return _rs_decode_payload(encoded)


def _read_pixel_stream(luma: bytes, width: int, height: int) -> bytes:
    """Read all 4080 logical bits from the LCG-scattered H2 pairs.

    Each bit majority-votes the up-to-4 pair deltas (bv - av). A pair whose
    chips fall off the canvas is skipped rather than cast; ties read as 0.
    """
    centers = gen_centers(RS_ENCODED_BYTES * 8 * PAIR_COUNT)
    out = bytearray(RS_ENCODED_BYTES)
    for bit_index in range(RS_ENCODED_BYTES * 8):
        diffs: list[float] = []
        for pair in range(PAIR_COUNT):
            cx, cy = centers[bit_index * PAIR_COUNT + pair]
            av = _sample_chip_luma(luma, width, height, cx - PAIR_OFFSET, cy)
            bv = _sample_chip_luma(luma, width, height, cx + PAIR_OFFSET, cy)
            if av is None or bv is None:
                continue
            diffs.append(bv - av)
        bit = 1 if diffs and sum(diffs) / len(diffs) > 0 else 0
        out[bit_index // 8] |= bit << (7 - (bit_index % 8))
    return bytes(out)


def _sample_chip_luma(
    luma: bytes, width: int, height: int, cx: int, cy: int
) -> float | None:
    total = 0
    count = 0
    for yy in (cy - 1, cy, cy + 1):
        if yy < 0 or yy >= height:
            continue
        base = yy * width
        for xx in (cx - 1, cx, cx + 1):
            if xx < 0 or xx >= width:
                continue
            total += luma[base + xx]
            count += 1
    return total / count if count else None


def _rs_encode_payload(payload_bytes: bytes) -> bytes:
    """RS-encode [magic 0x4D58][len 2B BE][payload] zero-padded to 2×135 bytes.

    Returns 510 bytes (2 RS(255,135) codewords). ``payload_bytes`` must be
    ≤ WM_MAX_PAYLOAD_BYTES (checked by callers).
    """
    data = WM_MAGIC + len(payload_bytes).to_bytes(2, "big") + payload_bytes
    data += b"\x00" * (RS_DATA_TOTAL - len(data))
    encoded = bytearray()
    for block in range(RS_BLOCK_COUNT):
        encoded += rs_encode_msg(data[block * RS_K : (block + 1) * RS_K], RS_NSYM)
    return bytes(encoded)


def _rs_decode_payload(encoded: bytes) -> bytes | None:
    """RS-decode the 510-byte carrier stream; return the payload or None.

    Every block must correct within its 60-byte budget; the decoded data must
    open with the magic and a plausible length. A watermark-less read (or a
    carrier corrupted past capacity) returns ``None``.
    """
    if len(encoded) != RS_ENCODED_BYTES:
        return None
    data = bytearray()
    for block in range(RS_BLOCK_COUNT):
        decoded = rs_correct_msg(encoded[block * 255 : (block + 1) * 255], RS_NSYM)
        if decoded is None:
            return None
        data += decoded[:RS_K]
    if data[:2] != WM_MAGIC:
        return None
    length = int.from_bytes(data[2:4], "big")
    if length <= 0 or length > WM_MAX_PAYLOAD_BYTES or len(data) < 4 + length:
        return None
    return bytes(data[4 : 4 + length])


def _payload_to_bits(payload_bytes: bytes) -> list[int]:
    encoded = _rs_encode_payload(payload_bytes)
    return [bit for byte in encoded for bit in ((byte >> shift) & 1 for shift in range(7, -1, -1))]


def _compact_json_bytes(obj: dict[str, object]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _png_text_chunk(*, keyword: bytes, text: bytes) -> bytes:
    chunk_data = keyword + b"\x00" + text
    return _png_chunk(b"tEXt", chunk_data)


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
