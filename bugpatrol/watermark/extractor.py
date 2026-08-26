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
import typing
import zlib

from PIL import Image, ImageDraw, UnidentifiedImageError

from bugpatrol.watermark.qr import extract_qr_envelope_bytes
from bugpatrol.watermark.rs256 import rs_correct_msg, rs_encode_msg
from bugpatrol.watermark.types import MAX_ENVELOPE_BYTES

CARRIER_START = b"BUGPATROL_WM1:"


class WatermarkInvalidEnvelope(Exception):
    """A watermark carrier was found but its contents are corrupt."""
CARRIER_END = b":BUGPATROL_WM1"
PNG_WATERMARK_KEYWORD = b"bugpatrol.watermark"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PIXEL_BIT_COLUMNS = 128
PIXEL_BIT_ROWS = 288
PIXEL_WIDTH_MODULES = PIXEL_BIT_COLUMNS * 2
PIXEL_HEIGHT_MODULES = PIXEL_BIT_ROWS
PIXEL_OFFSET_MODULES = 6
PIXEL_COPY_COUNT = 3
PIXEL_SCALE_CANDIDATES = tuple(1.0 + index * 0.125 for index in range(25))

# --- Error-corrected pixel carrier (RS(255,223) + 2D spread + coprime scramble)
#
# The 3× interleave alone failed on real screenshots: a wide horizontal UI band
# flips all three adjacent copies of a bit in the same row, and the old
# 16-bit length header / JSON payload had zero tolerance for even one wrong
# bit. The carrier now survives the real production channel (native render →
# downscale → JPEG re-encode) through three layers:
#
# 1. Reed-Solomon RS(255,223): the payload is [magic 0x4D57][len 2B BE][envelope]
#    zero-padded to 6×223 bytes, each block RS-encoded (32 parity bytes → up to
#    16 corrupted bytes per block corrected). 1530 encoded bytes total. (6 blocks
#    instead of 5 so the gzip'd dev-mode envelope fits: budget 1111B → 1334B.)
# 2. 2D-toroidal copy spread: copy `c` of scrambled bit `j` sits at cell index
#    `s + c*N` (N = 12240), so the three copies are ~95 rows + ~96 cols apart —
#    a single horizontal band or vertical edge flips at most ONE copy.
# 3. Coprime bit-scramble (K=8191): layout position `s = (j*K) % N` distributes
#    dense-UI-band byte errors evenly across all six RS blocks (each well under
#    the 16-byte budget), instead of clumping them into one fatal block.
#
# Grid layout (128 cols × 288 rows = 36864 cells; 36768 used):
#   positions 0..47       — 16-bit magic prefix, 0x4D57, 3 interleaved copies.
#                             Cheap flat/no-carrier bail for watermark-less
#                             images (0.2 ms vs a 165 ms full read).
#   positions 48..36767   — the 1530 RS-encoded bytes, scrambled + 2D spread.
#
# Constants MUST match the app's TypeScript builder
# (app/lib/dev/diagnosticScreenshotWatermarkPixels.ts) exactly.
_RS_NSYM = 32
RS_BLOCK_COUNT = 6
_RS_DATA_BYTES = 255 - _RS_NSYM  # 223
_RS_ENCODED_BYTES = RS_BLOCK_COUNT * 255  # 1530
_RS_MAGIC = b"\x4d\x57"
RS_DATA_TOTAL = RS_BLOCK_COUNT * _RS_DATA_BYTES  # 1338
PIXEL_MAGIC_BITS = 16
PIXEL_MAGIC_WORD = 0x4D57
PIXEL_MAGIC_CELLS = PIXEL_MAGIC_BITS * PIXEL_COPY_COUNT  # 48
PIXEL_LOGICAL_BITS = _RS_ENCODED_BYTES * 8  # 12240
PIXEL_DATA_CELLS = PIXEL_LOGICAL_BITS * PIXEL_COPY_COUNT  # 36720
PIXEL_CELL_TOTAL = PIXEL_MAGIC_CELLS + PIXEL_DATA_CELLS  # 36768
PIXEL_SCRAMBLE_K = 8191
PIXEL_SCRAMBLE_K_INV = 1711  # (K * K_INV) % PIXEL_LOGICAL_BITS == 1
# [magic 2B][len 2B] + payload, shared across the 6 RS data blocks.
PIXEL_MAX_ENVELOPE_BYTES = RS_DATA_TOTAL - 2 - 2  # 1334
# Magic-canary thresholds (see _read_carrier_bytes).
PIXEL_MAGIC_READABLE_MIN = 13
PIXEL_MAGIC_MISMATCH_CONFIDENT = 4
PIXEL_MAGIC_MISMATCH_BAIL = 8


def extract_envelope_bytes(data: bytes) -> bytes | None:
    """Return the compact JSON bytes of the embedded envelope, or None.

    Raises ``WatermarkInvalidEnvelope`` when a carrier is present but the
    payload inside it is not a valid base64-encoded JSON envelope.
    """
    candidates = list(iter_envelope_candidates(data))
    return candidates[0] if candidates else None


def extract_envelope_candidates(data: bytes) -> list[bytes]:
    """Every structurally-valid envelope embedded in ``data``, deduped.

    A pixel read at a wrong ±1px offset can yield JSON that *parses* as an
    envelope but carries corrupted ciphertext — valid-looking, wrong content.
    The decoder must not stop at the first parseable envelope: it tries each
    candidate against the private key (GCM auth is the ground truth) and the
    clean read wins. This is why the whole candidate set is surfaced.
    """
    return _dedupe(list(iter_envelope_candidates(data)))


def iter_envelope_candidates(data: bytes) -> typing.Iterator[bytes]:
    """Yield every structurally-valid embedded envelope, best-first.

    Lazy: the decoder pulls candidates one at a time and stops at the first
    that decrypts, so a clean screenshot costs a single read instead of the
    full scale/corner/offset probe space.
    """
    from_marker = _extract_from_markers(data)
    if from_marker is not None:
        yield from_marker
    if data.startswith(PNG_SIGNATURE):
        from_chunk = _extract_from_png_text_chunk(data)
        if from_chunk is not None:
            yield from_chunk
    yield from _iter_pixel_candidates(data)
    # QR/Data Matrix is the last resort: a visual barcode, scanned out of the
    # image rather than parsed out of the byte layout. Keep it last so byte
    # carriers stay canonical (they never degrade under re-encoding).
    from_qr = extract_qr_envelope_bytes(data)
    if from_qr is not None:
        yield from_qr


def _dedupe(envelopes: list[bytes]) -> list[bytes]:
    seen: set[bytes] = set()
    unique: list[bytes] = []
    for envelope in envelopes:
        if envelope not in seen:
            seen.add(envelope)
            unique.append(envelope)
    return unique


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
    ``corner="both"`` embeds the carrier at both corners like the app (top-left
    first, then bottom-right), giving the extractor two chances at a clean copy.
    """
    envelope_bytes = _compact_json_bytes(envelope)
    if len(envelope_bytes) > PIXEL_MAX_ENVELOPE_BYTES:
        raise ValueError("envelope is too large for screenshot pixel carrier")
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")
    cells = _pixel_cells(envelope_bytes)
    for current in (("top_left", "bottom_right") if corner == "both" else (corner,)):
        origin_x, origin_y = _pixel_origin(image.width, image.height, scale, current)
        origin_x += shift[0]
        origin_y += shift[1]
        for bit_index, bit in enumerate(cells):
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


def _iter_pixel_candidates(data: bytes) -> typing.Iterator[bytes]:
    """Yield pixel-carrier envelopes across scale/corner/offset, best-first.

    The app's fixed-3px cells decode at ``scale=3``; probe that first so a real
    screenshot succeeds on the first offset read. The remaining scales follow
    for re-encoded (downscaled) cell geometries.

    A scale stops probing once ANY corner reads a valid envelope: RS decoding
    is deterministic and rejects garbage reads, so the first RS-valid envelope
    at the app's nominal geometry IS the true payload — probing the mirrored
    corner or every scale would only re-find the same bytes.
    """
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        return
    # One C-pass luminance buffer for the whole probe space. Every cell sample
    # below is a flat-buffer index instead of a per-pixel PIL access + per-pixel
    # RGB->luma math, which is what keeps scale x corner x offset affordable.
    luma = _luma_buffer(image)
    for scale in _pixel_scale_order():
        if image.width < PIXEL_WIDTH_MODULES * scale or image.height < PIXEL_HEIGHT_MODULES * scale:
            continue
        for corner in ("top_left", "bottom_right"):
            found = False
            for decoded in _iter_pixel_carrier_candidates(image, luma=luma, scale=scale, corner=corner):
                found = True
                yield decoded
            if found:
                return


def _pixel_scale_order() -> tuple[float, ...]:
    return (3.0,) + tuple(scale for scale in PIXEL_SCALE_CANDIDATES if scale != 3.0)


def _iter_pixel_carrier_candidates(
    image: Image.Image, *, luma: bytes, scale: float, corner: str
) -> typing.Iterator[bytes]:
    """Yield the first offset whose read decodes to a valid envelope.

    The app renders cells at whole physical px, but a device's layout rounding
    can shift the grid by a pixel or two, so probe a small neighborhood of
    offsets nearest-first. ``_read_carrier_bytes`` only returns an envelope it
    RS-validated (garbage reads decode to ``None``), so the first offset that
    reads is the true payload — unlike the old JSON-parse candidates, an RS
    read cannot "parse but carry corrupted ciphertext".
    """
    origin_x, origin_y = _pixel_origin(image.width, image.height, scale, corner)
    for offset_x, offset_y in _carrier_offset_candidates():
        raw = _read_carrier_bytes(
            image, luma,
            origin_x=origin_x + offset_x,
            origin_y=origin_y + offset_y,
            scale=scale,
        )
        if raw is None:
            continue
        try:
            yield _decode_pixel_envelope(raw)
        except WatermarkInvalidEnvelope:
            continue
        return
    return


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


def _read_carrier_bytes(
    image: Image.Image, luma: bytes,
    *, origin_x: int, origin_y: int, scale: float,
) -> bytes | None:
    """RS-decode the error-corrected pixel carrier, or None if absent.

    Fast path first: read the 16-bit magic prefix (48 cells). A flat page
    leaves every cell unreadable (cheap bail — the hot path for watermark-less
    images); a busy page reads garbage whose majority magic is far from
    0x4D57 (confident no-carrier bail). Ambiguous magic (partially corrupted by
    the background) is double-checked with an RS-protected read of data block 0
    before paying for the full 30600-cell read.

    A carrier present at the right geometry reads the magic cleanly and is
    confirmed by RS decoding the full span; any residual bit errors (a band
    flipping cells) are corrected by RS(255,223) per block. Unreadable cells
    (JPEG washing a pair's luma delta below threshold) abstain during voting
    rather than killing the read.
    """
    magic_votes = [
        [
            _read_pixel_bit(image, luma, origin_x=origin_x, origin_y=origin_y, scale=scale, bit_index=3 * i + c)
            for c in range(PIXEL_COPY_COUNT)
        ]
        for i in range(PIXEL_MAGIC_BITS)
    ]
    readable = [i for i, votes in enumerate(magic_votes) if any(v is not None for v in votes)]
    if not readable:
        # Uniform grid region (flat page, wrong scale) — nothing to read.
        return None
    if len(readable) < PIXEL_MAGIC_READABLE_MIN:
        # Fewer than 13 of 16 magic bits readable: the grid is not aligned to
        # this scale/offset/corner. A real carrier reads its magic strip
        # cleanly (16/16) at the aligned geometry, and the extractor probes the
        # mirrored corner + ±1px offsets, so bail here instead of paying for an
        # RS-protected block-0 read that would only confirm the misalignment.
        return None
    mismatches = sum(
        1
        for i in readable
        if _majority_values([v for v in magic_votes[i] if v is not None])
        != ((PIXEL_MAGIC_WORD >> (PIXEL_MAGIC_BITS - 1 - i)) & 1)
    )
    if mismatches <= PIXEL_MAGIC_MISMATCH_CONFIDENT:
        return _rs_decode_payload(
            _read_pixel_logical_bits(image, luma, origin_x=origin_x, origin_y=origin_y, scale=scale)
        )
    if mismatches > PIXEL_MAGIC_MISMATCH_BAIL:
        # Confident no-carrier (busy background reading as non-magic).
        return None
    # Ambiguous magic (5-8 bits off): a background feature may have flipped it
    # while the carrier is present. RS-protected block 0 settles it cheaply
    # (~30 ms) before committing to the full ~165 ms read.
    if not _pixel_block0_valid(image, luma, origin_x=origin_x, origin_y=origin_y, scale=scale):
        return None
    return _rs_decode_payload(
        _read_pixel_logical_bits(image, luma, origin_x=origin_x, origin_y=origin_y, scale=scale)
    )


def _pixel_block0_valid(
    image: Image.Image, luma: bytes, *, origin_x: int, origin_y: int, scale: float
) -> bool:
    """True if RS-decoding data block 0 yields [magic 0x4D57][valid length].

    Reads only the 1784 logical bits of block 0 (5352 cells, ~30 ms vs ~165 ms
    for the full span) and lets RS(255,223) decide whether the carrier is
    really here — the same magic+length check the full read performs, but cheap
    enough to run as the ambiguous-magic safety net.
    """
    raw = bytearray(_RS_DATA_BYTES)
    for j in range(_RS_DATA_BYTES * 8):
        votes = _read_copy_votes(image, luma, origin_x=origin_x, origin_y=origin_y, scale=scale, logical_bit=j)
        if not votes:
            continue
        bit = _majority_values(votes)
        raw[j // 8] |= bit << (7 - (j % 8))
    decoded = rs_correct_msg(bytes(raw), _RS_NSYM)
    if decoded is None or decoded[:2] != _RS_MAGIC:
        return False
    length = int.from_bytes(decoded[2:4], "big")
    return 0 < length <= PIXEL_MAX_ENVELOPE_BYTES


def _read_pixel_logical_bits(
    image: Image.Image, luma: bytes, *, origin_x: int, origin_y: int, scale: float
) -> bytes:
    """Read all 10200 logical bits (3 copies each, unscrambled), majority-voted."""
    out = bytearray(_RS_ENCODED_BYTES)
    for s in range(PIXEL_LOGICAL_BITS):
        votes = _read_copy_votes_scrambled(
            image, luma, origin_x=origin_x, origin_y=origin_y, scale=scale, scrambled=s
        )
        if not votes:
            continue
        bit = _majority_values(votes)
        j = _pixel_unscramble(s)
        out[j // 8] |= bit << (7 - (j % 8))
    return bytes(out)


def _read_copy_votes(
    image: Image.Image, luma: bytes, *, origin_x: int, origin_y: int, scale: float, logical_bit: int
) -> list[int]:
    """The 3 copy votes for a scrambled logical bit (readable cells only)."""
    s = _pixel_scramble(logical_bit)
    return _read_copy_votes_scrambled(
        image, luma, origin_x=origin_x, origin_y=origin_y, scale=scale, scrambled=s
    )


def _read_copy_votes_scrambled(
    image: Image.Image, luma: bytes, *, origin_x: int, origin_y: int, scale: float, scrambled: int
) -> list[int]:
    """The 3 copy votes at grid position ``PIXEL_MAGIC_CELLS + s + c*N``.

    Copies are 10200 cells apart (~79 rows + ~88 cols), so a single horizontal
    band or vertical edge flips at most one of the three — majority voting then
    recovers the bit. JPEG-washed cells abstain (None), and a bit survives as
    long as at least one copy is readable.
    """
    votes: list[int] = []
    for c in range(PIXEL_COPY_COUNT):
        bit = _read_pixel_bit(
            image, luma,
            origin_x=origin_x, origin_y=origin_y, scale=scale,
            bit_index=PIXEL_MAGIC_CELLS + scrambled + c * PIXEL_LOGICAL_BITS,
        )
        if bit is not None:
            votes.append(bit)
    return votes


def _pixel_scramble(logical_bit: int) -> int:
    """Coprime permutation: layout position ``s`` for logical bit ``j``.

    A dense UI band corrupts a contiguous run of layout cells; unscrambling
    spreads those errors evenly across the five RS blocks (measured per-block
    errors [9,10,8,8,13] on a real screenshot vs [0,7,0,0,22] unscrambled) so
    no single block exceeds the 16-byte correction budget.
    """
    return (logical_bit * PIXEL_SCRAMBLE_K) % PIXEL_LOGICAL_BITS


def _pixel_unscramble(scrambled: int) -> int:
    return (scrambled * PIXEL_SCRAMBLE_K_INV) % PIXEL_LOGICAL_BITS


def _majority_values(bits: list[int]) -> int:
    return 1 if sum(bits) * 2 >= len(bits) else 0


def _read_pixel_bit(
    image: Image.Image,
    luma: bytes,
    *,
    origin_x: int,
    origin_y: int,
    scale: float,
    bit_index: int,
) -> int | None:
    x = origin_x + (bit_index % PIXEL_BIT_COLUMNS) * 2 * scale
    y = origin_y + (bit_index // PIXEL_BIT_COLUMNS) * scale
    left = _sample_cell_luma(luma, image.width, image.height, x, y, scale)
    right = _sample_cell_luma(luma, image.width, image.height, x + scale, y, scale)
    if left is None or right is None:
        return None
    delta = left - right
    if abs(delta) < 0.25:
        return None
    return 1 if delta < 0 else 0


def _sample_cell_luma(
    luma: bytes, width: int, height: int, x: float, y: float, scale: float
) -> float | None:
    start_x = max(0, math.floor(x))
    end_x = min(width - 1, math.ceil(x + scale) - 1)
    start_y = max(0, math.floor(y))
    end_y = min(height - 1, math.ceil(y + scale) - 1)
    if start_x > end_x or start_y > end_y:
        return None
    total = 0
    count = 0
    for py in range(start_y, end_y + 1):
        base = py * width
        for px in range(start_x, end_x + 1):
            total += luma[base + px]
            count += 1
    return total / count


def _luma_buffer(image: Image.Image) -> bytes:
    """Flat per-pixel luminance (0-255), indexed ``y * width + x``.

    Computed once per image as a single C pass; every later cell sample is a
    buffer index instead of a per-pixel PIL access + per-pixel RGB->luma math.
    (PIL's ``L`` uses ITU-R 601-2 luma vs the old Rec.709 weights — both are
    monotone positive linear combos, so cell deltas keep their sign and the
    0.25 readability threshold stays far below real signal.)
    """
    return image.convert("L").tobytes()


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


def _pixel_cells(envelope_bytes: bytes) -> list[int]:
    """Grid cells for the error-corrected carrier (magic prefix + RS data).

    Returns ``PIXEL_CELL_TOTAL`` cells: positions 0..47 hold the 16-bit magic
    (3 interleaved copies), positions 48..30647 hold the 1275 RS-encoded bytes
    scrambled + 2D-spread. Callers draw cell ``p`` at
    ``(p % 128, p // 128)`` grid coords — the same geometry the app renders.
    """
    cells = [0] * PIXEL_CELL_TOTAL
    for i in range(PIXEL_MAGIC_BITS):
        bit = (PIXEL_MAGIC_WORD >> (PIXEL_MAGIC_BITS - 1 - i)) & 1
        for c in range(PIXEL_COPY_COUNT):
            cells[3 * i + c] = bit
    encoded = _rs_encode_payload(envelope_bytes)
    for j, bit in enumerate(_bytes_to_logical_bits(encoded)):
        s = _pixel_scramble(j)
        for c in range(PIXEL_COPY_COUNT):
            cells[PIXEL_MAGIC_CELLS + s + c * PIXEL_LOGICAL_BITS] = bit
    return cells


def _rs_encode_payload(envelope_bytes: bytes) -> bytes:
    """RS-encode [magic][len 2B BE][envelope] zero-padded to 6×223 bytes.

    Returns 1530 bytes (6 RS(255,223) codewords). ``envelope_bytes`` must be
    ≤ PIXEL_MAX_ENVELOPE_BYTES (checked by callers).
    """
    data = _RS_MAGIC + len(envelope_bytes).to_bytes(2, "big") + envelope_bytes
    data += b"\x00" * (RS_DATA_TOTAL - len(data))
    encoded = bytearray()
    for block in range(RS_BLOCK_COUNT):
        encoded += rs_encode_msg(data[block * _RS_DATA_BYTES: (block + 1) * _RS_DATA_BYTES], _RS_NSYM)
    return bytes(encoded)


def _rs_decode_payload(encoded: bytes) -> bytes | None:
    """RS-decode the 1530-byte carrier stream; return the envelope or None.

    Every block must correct within its 16-byte budget; the decoded data must
    open with the magic and a plausible length. A watermark-less read (or a
    carrier corrupted past capacity) returns ``None``.
    """
    if len(encoded) != _RS_ENCODED_BYTES:
        return None
    data = bytearray()
    for block in range(RS_BLOCK_COUNT):
        decoded = rs_correct_msg(encoded[block * 255: (block + 1) * 255], _RS_NSYM)
        if decoded is None:
            return None
        data += decoded[:_RS_DATA_BYTES]
    if data[:2] != _RS_MAGIC:
        return None
    length = int.from_bytes(data[2:4], "big")
    if length <= 0 or length > PIXEL_MAX_ENVELOPE_BYTES or len(data) < 4 + length:
        return None
    return bytes(data[4:4 + length])


def _bytes_to_logical_bits(data: bytes) -> list[int]:
    return [bit for byte in data for bit in ((byte >> shift) & 1 for shift in range(7, -1, -1))]


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
