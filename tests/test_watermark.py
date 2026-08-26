"""Watermark decode: deterministic extraction, decryption, and pipeline wiring.

Covers the required failure modes:

- no watermark image            -> found:false, watermark_not_found
- valid fixture image           -> decoded payload (all core fields)
- wrong/missing private key     -> fails visibly with a distinct error code
- unknown keyId                 -> fails visibly (rotation gap)
- corrupted payload / envelope  -> fails visibly

plus the triage-pipeline integration (materialize -> intake render ->
media-evidence extraction -> triage context) and the CLI contract.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bugpatrol.__main__ import main
from bugpatrol.clients import GitHubIssue
from bugpatrol.intake import (
    Attachment,
    IntakeRecord,
    intake_record_from_dict,
    render_attachments_markdown,
)
from bugpatrol.lark import DownloadedLarkResource
from bugpatrol.resources import LocalResourceStore, materialize_attachment
from bugpatrol.triage_context import (
    MediaEvidence,
    TriageContext,
    build_triage_context,
    extract_media_evidence,
    render_triage_context_markdown,
    resolve_media_watermarks,
)
from bugpatrol.watermark import (
    DEFAULT_KEY_ID,
    ENV_KEYS_JSON,
    ENV_PRIVATE_KEY,
    ERROR_BAD_ENVELOPE,
    ERROR_DECRYPT,
    ERROR_KEY_MISSING,
    ERROR_KEY_UNKNOWN,
    ERROR_NOT_FOUND,
    NO_WATERMARK_NOTE,
    WatermarkBadPayload,
    WatermarkKeyStore,
    build_envelope,
    candidates_to_compact_json,
    decode_image,
    decrypt_envelope,
    embed_envelope_trailer,
    embed_png_text_envelope,
    embed_screenshot_pixel_envelope,
    extract_envelope_candidates,
    payload_to_compact_json,
    watermark_failure_note,
)
from bugpatrol.watermark.extractor import PIXEL_MAX_ENVELOPE_BYTES
from bugpatrol.watermark.keys import (
    WatermarkBadKeyConfig,
    WatermarkKeyNotFound,
)


def _keypair() -> tuple[str, str]:
    """(private_pem, public_pem) for a fresh RSA-2048 key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_pem, public_pem


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schemaVersion": 1,
        "keyId": DEFAULT_KEY_ID,
        "watermarkId": "wm-abc123",
        "uid": "u_12345",
        "pathname": "/settings/account",
        "platform": "ios",
        "appVersion": "1.2.3",
        "buildVersion": "42",
        "buildInfo": "Debug",
        "gitCommit": "deadbeef",
        "buildTime": "2026-08-25T00:00:00Z",
        "modelName": "iPhone 15",
        "osName": "iOS",
        "osVersion": "18.5",
        "capturedAt": "2026-08-25T08:00:00Z",
    }
    base.update(overrides)
    return base


def _envelope_with_plaintext(public_pem: str, plaintext: bytes) -> dict[str, object]:
    """Build an envelope whose AES plaintext is exactly ``plaintext`` bytes.

    Lets tests mint a corrupt-gzip / arbitrary-plaintext envelope directly
    (``build_envelope`` always gzips, so it cannot inject raw plaintext).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    aes_key = b"\x01" * 32
    iv = b"\x02" * 12
    ciphertext_and_tag = AESGCM(aes_key).encrypt(iv, plaintext, None)
    public_key = load_pem_public_key(public_pem.encode("utf-8"))
    if not isinstance(public_key, RSAPublicKey):
        raise ValueError("not an RSA public key")
    wrapped_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "v": 1,
        "keyId": DEFAULT_KEY_ID,
        "alg": "RSA-OAEP-256+AES-256-GCM",
        "data": {
            "ciphertext": base64.b64encode(ciphertext_and_tag[:-16]).decode("ascii"),
            "iv": base64.b64encode(iv).decode("ascii"),
            "tag": base64.b64encode(ciphertext_and_tag[-16:]).decode("ascii"),
            "wrappedKey": base64.b64encode(wrapped_key).decode("ascii"),
        },
    }


def _png_1x1() -> bytes:
    """A minimal but structurally valid 1x1 RGBA PNG."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    idat = _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
    iend = _png_chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def _png_canvas(width: int = 900, height: int = 600) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), (142, 137, 129))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return length + chunk_type + data + struct.pack(">I", crc)


def _invert_encoded_byte(image, *, scale: float, corner: str, encoded_pos: int) -> None:
    """Flip every cell (all 3 copies) of one encoded byte, inverting its value.

    Stages byte errors for the RS-budget tests: flipping all 3 copies guarantees
    the majority vote flips, so each call is exactly one corrupted byte in the
    carrier's RS(255,223) codeword (the magic prefix cells are untouched, so the
    canary still reads clean and the full read is what exercises RS).
    """
    from bugpatrol.watermark import extractor as _ext

    origin_x, origin_y = _ext._pixel_origin(image.width, image.height, scale, corner)
    for c in range(_ext.PIXEL_COPY_COUNT):
        for j in range(encoded_pos * 8, encoded_pos * 8 + 8):
            s = _ext._pixel_scramble(j)
            cell = _ext.PIXEL_MAGIC_CELLS + s + c * _ext.PIXEL_LOGICAL_BITS
            x = origin_x + (cell % _ext.PIXEL_BIT_COLUMNS) * 2 * scale
            y = origin_y + (cell // _ext.PIXEL_BIT_COLUMNS) * scale
            left = image.crop((x, y, x + scale, y + scale))
            right = image.crop((x + scale, y, x + 2 * scale, y + scale))
            image.paste(right, (x, y))
            image.paste(left, (x + scale, y))


try:
    import qrcode
except ImportError:  # pragma: no cover - test-only QR encoder, optional
    qrcode = None


def _qr_png(text: str) -> bytes:
    """Render ``text`` as a QR PNG (ecl M, matching the app's badge)."""
    if qrcode is None:
        raise unittest.SkipTest("qrcode encoder not installed")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _qr_screenshot(*, watermark_text: str | None, other_text: str | None) -> bytes:
    """Synthetic screenshot: watermark QR at top-left (app badge) and/or an
    unrelated QR at bottom-right (e.g. a share card shown on screen)."""
    from PIL import Image

    canvas = Image.new("RGB", (900, 600), (142, 137, 129))
    if other_text is not None:
        other = Image.open(io.BytesIO(_qr_png(other_text))).convert("RGB").resize((220, 220))
        canvas.paste(other, (900 - 220 - 12, 600 - 220 - 12))
    if watermark_text is not None:
        wm = Image.open(io.BytesIO(_qr_png(watermark_text))).convert("RGB").resize((200, 200))
        canvas.paste(wm, (12, 12))
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


class WatermarkDecodeTest(unittest.TestCase):
    """Required failure modes + round-trip through both carriers."""

    private_pem: str
    public_pem: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.private_pem, cls.public_pem = _keypair()

    def _store(self) -> WatermarkKeyStore:
        return WatermarkKeyStore(keys={DEFAULT_KEY_ID: self.private_pem})

    def _envelope(self, **overrides: object) -> dict[str, object]:
        return build_envelope(
            _payload(),
            public_key_pem=self.public_pem,
            key_id=DEFAULT_KEY_ID,
        )

    def test_no_watermark_returns_not_found(self) -> None:
        result = decode_image(_png_1x1(), key_store=self._store())
        self.assertFalse(result.found)
        self.assertEqual(result.confidence, 0)
        self.assertEqual(result.error, ERROR_NOT_FOUND)

    def test_trailer_carrier_round_trips_full_payload(self) -> None:
        embedded = embed_envelope_trailer(_png_1x1(), self._envelope())
        result = decode_image(embedded, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.key_id, DEFAULT_KEY_ID)
        self.assertEqual(result.payload, _payload())

    def test_png_text_chunk_carrier_round_trips(self) -> None:
        embedded = embed_png_text_envelope(_png_1x1(), self._envelope())
        result = decode_image(embedded, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_screenshot_pixel_carrier_round_trips(self) -> None:
        embedded = embed_screenshot_pixel_envelope(_png_canvas(), self._envelope(), scale=2)
        result = decode_image(embedded, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_screenshot_pixel_carrier_handles_fractional_android_scale(self) -> None:
        embedded = embed_screenshot_pixel_envelope(_png_canvas(width=1200, height=900), self._envelope(), scale=2.625)
        result = decode_image(embedded, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_screenshot_pixel_carrier_fixed_3px_phone_scale_with_layout_rounding(self) -> None:
        # The app renders cells at fixed 3 physical px (DPR-independent), which
        # the extractor reads at scale=3. A device's layout rounding can shift
        # the grid by a pixel; the extractor's ±1px offset probe absorbs it.
        base = _png_canvas(width=1080, height=2340)
        aligned = embed_screenshot_pixel_envelope(base, self._envelope(), scale=3)
        result = decode_image(aligned, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())
        for shift in ((1, 0), (0, 1), (-1, -1)):
            shifted = embed_screenshot_pixel_envelope(base, self._envelope(), scale=3, shift=shift)
            result = decode_image(shifted, key_store=self._store())
            self.assertTrue(result.found, msg=f"shift={shift} should still decode")
            self.assertEqual(result.payload, _payload())

    def test_screenshot_pixel_carrier_recovers_a_background_step_via_majority(self) -> None:
        """A sharp vertical UI edge under the carrier inverts every cell pair it
        straddles (the >13-luma step between the two cells flips the polarity).
        The 3× bit-interleaved copies majority-vote the flipped bits back."""
        from PIL import Image, ImageDraw

        envelope = self._envelope()
        image = Image.open(io.BytesIO(_png_canvas(width=1080, height=2340))).convert("RGB")
        draw = ImageDraw.Draw(image)
        # Dark vertical stripe across the carrier region, drawn as background
        # before the carrier (the app renders the overlay last).
        draw.rectangle((300, 0, 306, 2340), fill=(30, 32, 36))
        out = io.BytesIO()
        image.save(out, format="PNG")
        embedded = embed_screenshot_pixel_envelope(out.getvalue(), envelope, scale=3, corner="both")
        result = decode_image(embedded, key_store=self._store())
        self.assertTrue(result.found, msg="background step should be majority-recovered")
        self.assertEqual(result.payload, _payload())

    def test_screenshot_pixel_carrier_survives_ui_texture_via_majority_and_corners(self) -> None:
        """Real screenshots have sharp UI edges under the carrier: a texture with
        luma steps >13 between adjacent 3px cells flips individual cells'
        polarity. The 3× copy majority plus both-corner redundancy must still
        decode the envelope. Texture is drawn under the carrier (the app renders
        the overlay last)."""
        from PIL import Image, ImageDraw

        envelope = self._envelope()
        image = Image.open(io.BytesIO(_png_canvas(width=1080, height=2340))).convert("RGB")
        draw = ImageDraw.Draw(image)
        for y in range(40, 2300, 140):  # horizontal text/separator runs
            draw.rectangle((20, y, 700, y + 26), fill=(230, 231, 236))
        for x in range(60, 1060, 260):  # dark chips
            draw.rectangle((x, 200, x + 130, 230), fill=(60, 64, 72))
        for (x, y) in ((300, 500), (500, 900), (700, 1300)):  # avatars/badges
            draw.ellipse((x, y, x + 60, y + 60), fill=(200, 60, 60))
        out = io.BytesIO()
        image.save(out, format="PNG")
        textured = out.getvalue()
        embedded = embed_screenshot_pixel_envelope(textured, envelope, scale=3, corner="both")
        result = decode_image(embedded, key_store=self._store())
        self.assertTrue(result.found, msg="textured background should still decode")
        self.assertEqual(result.payload, _payload())

    def test_screenshot_pixel_carrier_survives_jpeg_q85_transcode(self) -> None:
        """The pipeline transcodes screenshots to JPEG q85, whose quantization
        washes some 3px cell pairs' luma delta below the detection threshold.
        Unreadable cells must abstain (per-bit majority over the 3 interleaved
        copies) instead of discarding the whole carrier."""
        from PIL import Image

        base = _png_canvas(width=1080, height=2340)
        embedded = embed_screenshot_pixel_envelope(base, self._envelope(), scale=3, corner="both")
        jpeg = io.BytesIO()
        Image.open(io.BytesIO(embedded)).save(jpeg, format="JPEG", quality=85)
        result = decode_image(jpeg.getvalue(), key_store=self._store())
        self.assertTrue(result.found, msg="JPEG q85 transcode should still decode")
        self.assertEqual(result.payload, _payload())

    def test_screenshot_pixel_carrier_survives_ui_edge_and_jpeg(self) -> None:
        """A UI edge inverts cell polarity AND JPEG washes cells out; the
        3× majority + corner redundancy + unreadable-cell abstention together
        must still recover the envelope."""
        from PIL import Image, ImageDraw

        envelope = self._envelope()
        image = Image.open(io.BytesIO(_png_canvas(width=1080, height=2340))).convert("RGB")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 60, 2340), fill=(40, 42, 50))
        draw.rectangle((40, 140, 400, 168), fill=(90, 92, 100))
        draw.rectangle((40, 180, 520, 204), fill=(70, 72, 80))
        out = io.BytesIO()
        image.save(out, format="PNG")
        embedded = embed_screenshot_pixel_envelope(out.getvalue(), envelope, scale=3, corner="both")
        jpeg = io.BytesIO()
        Image.open(io.BytesIO(embedded)).save(jpeg, format="JPEG", quality=85)
        result = decode_image(jpeg.getvalue(), key_store=self._store())
        self.assertTrue(result.found, msg="UI edge + JPEG should still decode")
        self.assertEqual(result.payload, _payload())

    def test_pixel_cells_magic_prefix_layout(self) -> None:
        """The 16-bit magic word 0x4D57 occupies cells 0..47, bit i replicated
        3× at positions 3i, 3i+1, 3i+2 — the canary the extractor reads first."""
        from bugpatrol.watermark import extractor as _ext

        payload = b"\x4d\x57" + b"\x00" * _ext.PIXEL_MAX_ENVELOPE_BYTES
        cells = _ext._pixel_cells(payload)
        self.assertEqual(len(cells), _ext.PIXEL_CELL_TOTAL)
        for i in range(_ext.PIXEL_MAGIC_BITS):
            bit = (_ext.PIXEL_MAGIC_WORD >> (_ext.PIXEL_MAGIC_BITS - 1 - i)) & 1
            for c in range(_ext.PIXEL_COPY_COUNT):
                self.assertEqual(cells[3 * i + c], bit)

    def test_pixel_scramble_is_a_permutation(self) -> None:
        """K=8191 is coprime to 10200, so scramble/unscramble are inverses over
        the whole logical-bit space (the 2D spread depends on it)."""
        from bugpatrol.watermark import extractor as _ext

        for j in range(_ext.PIXEL_LOGICAL_BITS):
            self.assertEqual(_ext._pixel_unscramble(_ext._pixel_scramble(j)), j)

    def test_pixel_carrier_corrects_up_to_16_byte_errors_per_block(self) -> None:
        """RS(255,223) budget: 8 and 16 corrupted bytes in data block 0 still
        decode (single corner, so there is no mirrored copy to lean on); 17
        bytes is uncorrectable and the carrier is rejected outright."""
        from PIL import Image

        embedded = embed_screenshot_pixel_envelope(
            _png_canvas(width=1080, height=2340), self._envelope(), scale=3, corner="top_left"
        )
        for nbytes in (8, 16):
            image = Image.open(io.BytesIO(embedded)).convert("RGB")
            for pos in range(nbytes):
                _invert_encoded_byte(image, scale=3, corner="top_left", encoded_pos=pos)
            out = io.BytesIO()
            image.save(out, format="PNG")
            result = decode_image(out.getvalue(), key_store=self._store())
            self.assertTrue(result.found, msg=f"{nbytes} corrupted bytes should correct")
            self.assertEqual(result.payload, _payload())
        image = Image.open(io.BytesIO(embedded)).convert("RGB")
        for pos in range(17):
            _invert_encoded_byte(image, scale=3, corner="top_left", encoded_pos=pos)
        out = io.BytesIO()
        image.save(out, format="PNG")
        self.assertEqual(extract_envelope_candidates(out.getvalue()), [])

    def test_flat_page_bails_via_magic_canary(self) -> None:
        """A uniform screenshot leaves every canary cell unreadable: the cheap
        flat bail returns no candidates without touching the RS full read."""
        self.assertEqual(extract_envelope_candidates(_png_canvas(width=1080, height=2340)), [])

    def test_qr_carrier_round_trips(self) -> None:
        envelope = self._envelope()
        screenshot = _qr_screenshot(
            watermark_text=json.dumps(envelope, separators=(",", ":")),
            other_text=None,
        )
        result = decode_image(screenshot, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.key_id, DEFAULT_KEY_ID)
        self.assertEqual(result.payload, _payload())

    def test_qr_carrier_accepts_base64_wrapped_envelope(self) -> None:
        envelope = self._envelope()
        wrapped = base64.b64encode(
            json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        screenshot = _qr_screenshot(watermark_text=wrapped, other_text=None)
        result = decode_image(screenshot, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_qr_carrier_unrelated_on_screen_qr_is_not_a_watermark(self) -> None:
        screenshot = _qr_screenshot(
            watermark_text=None,
            other_text="https://example.com/share/abc",
        )
        result = decode_image(screenshot, key_store=self._store())
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_NOT_FOUND)

    def test_qr_carrier_wins_over_unrelated_on_screen_qr_by_content(self) -> None:
        # The app badge (top-left) and a share card (bottom-right) coexist on
        # screen; only the envelope-JSON QR is a watermark, so content-based
        # selection must pick it even though the other QR is also present.
        envelope = self._envelope()
        screenshot = _qr_screenshot(
            watermark_text=json.dumps(envelope, separators=(",", ":")),
            other_text="https://example.com/share/abc",
        )
        result = decode_image(screenshot, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_decode_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screenshot.png"
            path.write_bytes(embed_envelope_trailer(_png_1x1(), self._envelope()))
            result = decode_image(path, key_store=self._store())
        self.assertTrue(result.found)

    def test_missing_private_key_fails_visibly(self) -> None:
        result = decode_image(
            embed_envelope_trailer(_png_1x1(), self._envelope()),
            key_store=WatermarkKeyStore(),  # feature off
        )
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_KEY_MISSING)

    def test_wrong_private_key_fails_visibly(self) -> None:
        _, other_public = _keypair()
        envelope = build_envelope(
            _payload(),
            public_key_pem=other_public,
            key_id=DEFAULT_KEY_ID,
        )
        result = decode_image(
            embed_envelope_trailer(_png_1x1(), envelope),
            key_store=self._store(),  # this private key is the WRONG one
        )
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_DECRYPT)

    def test_unknown_key_id_fails_visibly(self) -> None:
        envelope = build_envelope(
            _payload(keyId="retired-key-v0"),
            public_key_pem=self.public_pem,
            key_id="retired-key-v0",
        )
        result = decode_image(
            embed_envelope_trailer(_png_1x1(), envelope),
            key_store=self._store(),  # only knows diagnostic-watermark-v1
        )
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_KEY_UNKNOWN)

    def test_rotation_keys_store_decrypts_old_key_id(self) -> None:
        old_private, old_public = _keypair()
        envelope = build_envelope(
            _payload(keyId="retired-key-v0"),
            public_key_pem=old_public,
            key_id="retired-key-v0",
        )
        store = WatermarkKeyStore(
            keys={DEFAULT_KEY_ID: self.private_pem, "retired-key-v0": old_private}
        )
        result = decode_image(
            embed_envelope_trailer(_png_1x1(), envelope),
            key_store=store,
        )
        self.assertTrue(result.found)
        self.assertEqual(result.key_id, "retired-key-v0")

    def test_corrupted_ciphertext_fails_visibly(self) -> None:
        envelope = self._envelope()
        data = envelope["data"]
        assert isinstance(data, dict)
        ciphertext = data["ciphertext"]
        assert isinstance(ciphertext, str)
        data = dict(data)
        data["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        envelope = dict(envelope)
        envelope["data"] = data
        result = decode_image(
            embed_envelope_trailer(_png_1x1(), envelope),
            key_store=self._store(),
        )
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_DECRYPT)

    def test_corrupted_envelope_fails_visibly(self) -> None:
        # A carrier whose base64 is not valid -> watermark_invalid_envelope.
        bad = _png_1x1() + b"BUGPATROL_WM1:%%%not-base64%%%:BUGPATROL_WM1"
        result = decode_image(bad, key_store=self._store())
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_BAD_ENVELOPE)

    def test_truncated_trailer_fails_visibly(self) -> None:
        bad = _png_1x1() + b"BUGPATROL_WM1:aaaa"
        result = decode_image(bad, key_store=self._store())
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_BAD_ENVELOPE)

    def test_bad_payload_schema_version_fails_visibly(self) -> None:
        envelope = build_envelope(
            _payload(schemaVersion=99),
            public_key_pem=self.public_pem,
            key_id=DEFAULT_KEY_ID,
        )
        result = decode_image(
            embed_envelope_trailer(_png_1x1(), envelope),
            key_store=self._store(),
        )
        self.assertFalse(result.found)
        self.assertEqual(result.error, "watermark_invalid_payload")


class WatermarkCompressionTest(unittest.TestCase):
    """Payload gzip compression (RFC1952) before AES-GCM.

    The app compresses the payload JSON so the dev-mode payload (with its extra
    testing fields) fits the pixel carrier budget; the decryptor transparently
    decompresses and still accepts legacy uncompressed plaintext.
    """

    private_pem: str
    public_pem: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.private_pem, cls.public_pem = _keypair()

    def _store(self) -> WatermarkKeyStore:
        return WatermarkKeyStore(keys={DEFAULT_KEY_ID: self.private_pem})

    def test_gzip_compressed_payload_round_trips(self) -> None:
        envelope = build_envelope(
            _payload(),
            public_key_pem=self.public_pem,
            key_id=DEFAULT_KEY_ID,
        )
        embedded = embed_screenshot_pixel_envelope(_png_canvas(), envelope, scale=2)
        result = decode_image(embedded, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_legacy_uncompressed_payload_still_decrypts(self) -> None:
        envelope = build_envelope(
            _payload(),
            public_key_pem=self.public_pem,
            key_id=DEFAULT_KEY_ID,
            compress=False,
        )
        embedded = embed_screenshot_pixel_envelope(_png_canvas(), envelope, scale=2)
        result = decode_image(embedded, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_corrupt_gzip_plaintext_fails_as_bad_payload(self) -> None:
        envelope = _envelope_with_plaintext(
            self.public_pem,
            b"\x1f\x8b" + b"definitely-not-a-valid-gzip-stream-xxxxxxxx",
        )
        with self.assertRaises(WatermarkBadPayload):
            decrypt_envelope(envelope, self._store())

    def test_dev_mode_payload_fits_pixel_carrier_budget(self) -> None:
        """Regression for the 1420B > 1111B dev-build overflow: the full dev
        payload (testing fields included) must fit the pixel carrier budget,
        and it must still pixel-embed + decode end to end."""
        payload = _payload(
            nickname="Tester",
            socialId="tester_01",
            pageDebug={"uid": "99", "sessionId": "buddy-session", "threadId": "123", "isBuddy": "true"},
            buildInfoSecondary="feature-watermark",
            gitBranch="sys-wm",
            manufacturer="Apple",
            rawDeviceId="hashed-device-id",
            rawDeviceIdThree="shumei-device-id",
            wsStatus="open",
            inflightRequests=2,
            timezone="Asia/Shanghai",
        )
        envelope = build_envelope(payload, public_key_pem=self.public_pem, key_id=DEFAULT_KEY_ID)
        envelope_bytes = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(envelope_bytes), PIXEL_MAX_ENVELOPE_BYTES)
        embedded = embed_screenshot_pixel_envelope(
            _png_canvas(width=1080, height=2340), envelope, scale=3,
        )
        result = decode_image(embedded, key_store=self._store())
        self.assertTrue(result.found)
        self.assertEqual(result.payload, payload)


class WatermarkKeyStoreTest(unittest.TestCase):
    def test_from_env_reads_primary_and_rotation(self) -> None:
        private, _ = _keypair()
        rotation_private, _ = _keypair()
        with patch.dict(
            os.environ,
            {
                ENV_PRIVATE_KEY: private,
                ENV_KEYS_JSON: json.dumps({"old-key": rotation_private}),
            },
            clear=True,
        ):
            store = WatermarkKeyStore.from_env()
        self.assertEqual(set(store.key_ids()), {DEFAULT_KEY_ID, "old-key"})
        self.assertEqual(store.resolve(DEFAULT_KEY_ID), private.strip())
        self.assertEqual(store.resolve("old-key"), rotation_private.strip())

    def test_from_env_malformed_keys_json_raises(self) -> None:
        with patch.dict(os.environ, {ENV_KEYS_JSON: "not json", ENV_PRIVATE_KEY: ""}, clear=True):
            with self.assertRaises(WatermarkBadKeyConfig):
                WatermarkKeyStore.from_env()

    def test_empty_env_is_feature_off(self) -> None:
        with patch.dict(os.environ, {ENV_PRIVATE_KEY: "", ENV_KEYS_JSON: ""}, clear=True):
            store = WatermarkKeyStore.from_env()
        self.assertFalse(store.has_keys())

    def test_resolve_unknown_key_raises(self) -> None:
        private, _ = _keypair()
        store = WatermarkKeyStore(keys={DEFAULT_KEY_ID: private})
        with self.assertRaises(WatermarkKeyNotFound):
            store.resolve("no-such-key")


class WatermarkPipelineTest(unittest.TestCase):
    """Candidates ride through materialization, intake render, and triage render."""

    private_pem: str
    public_pem: str
    watermarked_png: bytes
    compact: str
    candidates_json: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.private_pem, cls.public_pem = _keypair()
        cls.envelope = build_envelope(
            _payload(),
            public_key_pem=cls.public_pem,
            key_id=DEFAULT_KEY_ID,
        )
        cls.watermarked_png = embed_envelope_trailer(_png_1x1(), cls.envelope)
        cls.compact = payload_to_compact_json(_payload())
        envelope_bytes = json.dumps(cls.envelope, separators=(",", ":")).encode("utf-8")
        cls.candidates_json = candidates_to_compact_json([envelope_bytes])

    def _store(self) -> WatermarkKeyStore:
        return WatermarkKeyStore(keys={DEFAULT_KEY_ID: self.private_pem})

    def _watermarked_attachment(self) -> Attachment:
        return Attachment(
            kind="image",
            url="lark://message/om_wm/image/img_v2_wm",
        )

    def test_materialize_attachment_extracts_candidates(self) -> None:
        class Downloader:
            def download_message_resource(self, **kwargs: object) -> DownloadedLarkResource:
                return DownloadedLarkResource(
                    content=WatermarkPipelineTest.watermarked_png,
                    content_type="image/png",
                    filename="bug screenshot.png",
                )

        downloader = Downloader()
        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_attachment(
                attachment=self._watermarked_attachment(),
                lark=downloader,
                store=LocalResourceStore(Path(tmp)),
            )
        self.assertEqual(materialized.watermark, self.candidates_json)

    def test_materialize_extracts_before_transform_strips_carrier(self) -> None:
        # A redactor that rewrites the bytes (as a JPEG re-encode would) must not
        # lose the watermark candidates, because extraction runs on the ORIGINAL
        # bytes first (keyless — the runner decrypts later).
        class StrippingRedactor:
            def redact(self, *, ref: object, resource: DownloadedLarkResource) -> DownloadedLarkResource:
                return DownloadedLarkResource(
                    content=b"stripped-bytes",
                    content_type=resource.content_type,
                    filename=resource.filename,
                )

        class Downloader:
            def download_message_resource(self, **kwargs: object) -> DownloadedLarkResource:
                return DownloadedLarkResource(
                    content=WatermarkPipelineTest.watermarked_png,
                    content_type="image/png",
                    filename="bug screenshot.png",
                )

        downloader = Downloader()
        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_attachment(
                attachment=self._watermarked_attachment(),
                lark=downloader,
                store=LocalResourceStore(Path(tmp)),
                redactor=StrippingRedactor(),
            )
        self.assertEqual(materialized.watermark, self.candidates_json)

    def test_materialize_video_without_watermark_reports_not_found(self) -> None:
        # Videos are watermark candidates too (the trailer carrier can ride any
        # media), so a clean video must surface an explicit "checked, absent"
        # note instead of silently omitting the watermark line.
        class Downloader:
            def download_message_resource(self, **kwargs: object) -> DownloadedLarkResource:
                return DownloadedLarkResource(
                    content=b"clean-video-bytes",
                    content_type="video/mp4",
                    filename="repro.mp4",
                )

        attachment = materialize_attachment(
            attachment=Attachment(
                kind="video",
                url="lark://message/om_wm/video/img_v2_wm",
            ),
            lark=Downloader(),
            store=LocalResourceStore(Path(tempfile.mkdtemp())),
        )
        self.assertEqual(attachment.watermark, NO_WATERMARK_NOTE)

    def test_materialize_image_without_watermark_reports_not_found(self) -> None:
        class Downloader:
            def download_message_resource(self, **kwargs: object) -> DownloadedLarkResource:
                return DownloadedLarkResource(
                    content=_png_1x1(),
                    content_type="image/png",
                    filename="clean.png",
                )

        attachment = materialize_attachment(
            attachment=self._watermarked_attachment(),
            lark=Downloader(),
            store=LocalResourceStore(Path(tempfile.mkdtemp())),
        )
        self.assertEqual(attachment.watermark, NO_WATERMARK_NOTE)

    def test_render_attachments_markdown_emits_candidates_line(self) -> None:
        copy = {
            "open_asset": "open asset",
            "preview": "preview",
            "image_alt": "image",
            "generated_description": "generated description",
            "none": "none",
        }
        markdown = render_attachments_markdown(
            (Attachment(kind="image", url="https://assets/x.png", watermark=self.candidates_json),),
            copy=copy,
        )
        self.assertIn(f"- watermark-candidates: {self.candidates_json}", markdown)

    def test_render_attachments_markdown_emits_not_found_note(self) -> None:
        copy = {
            "open_asset": "open asset",
            "preview": "preview",
            "image_alt": "image",
            "generated_description": "generated description",
            "none": "none",
        }
        markdown = render_attachments_markdown(
            (Attachment(kind="image", url="https://assets/x.png", watermark=NO_WATERMARK_NOTE),),
            copy=copy,
        )
        self.assertIn(f"- watermark: {NO_WATERMARK_NOTE}", markdown)

    def test_intake_record_from_dict_round_trips_candidates(self) -> None:
        record = IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-08-25T00:00:00Z",
            chat_id="oc_1",
            root_id="om_1",
            message_id="om_1",
            original_text="bug",
            attachments=(Attachment(kind="image", url="u", watermark=self.candidates_json),),
        )
        as_dict = {
            "reporter_name": record.reporter_name,
            "reporter_open_id": record.reporter_open_id,
            "created_at": record.created_at,
            "chat_id": record.chat_id,
            "root_id": record.root_id,
            "message_id": record.message_id,
            "original_text": record.original_text,
            "attachments": [vars(a) for a in record.attachments],
        }
        restored = intake_record_from_dict(as_dict)
        self.assertEqual(restored.attachments[0].watermark, self.candidates_json)

    def test_intake_record_from_dict_round_trips_not_found_note(self) -> None:
        attachments = (Attachment(kind="image", url="u", watermark=NO_WATERMARK_NOTE),)
        as_dict = {
            "reporter_name": "Reporter",
            "reporter_open_id": "ou_1",
            "created_at": "2026-08-25T00:00:00Z",
            "chat_id": "oc_1",
            "root_id": "om_1",
            "message_id": "om_1",
            "original_text": "bug",
            "attachments": [vars(a) for a in attachments],
        }
        restored = intake_record_from_dict(as_dict)
        self.assertEqual(restored.attachments[0].watermark, NO_WATERMARK_NOTE)

    def test_extract_media_evidence_parses_candidates_line(self) -> None:
        markdown = (
            "- image: https://assets/x.png\n"
            f"  - watermark-candidates: {self.candidates_json}\n"
        )
        evidence = extract_media_evidence(markdown)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].watermark, self.candidates_json)

    def test_extract_media_evidence_parses_not_found_note(self) -> None:
        markdown = (
            "- image: https://assets/x.png\n"
            f"  - watermark: {NO_WATERMARK_NOTE}\n"
        )
        evidence = extract_media_evidence(markdown)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].watermark, NO_WATERMARK_NOTE)

    def test_render_triage_context_markdown_summarizes_candidates(self) -> None:
        # Without a key the triage agent must see a compact count, not the raw
        # encrypted JSON array dumped into the prompt.
        issue = GitHubIssue(
            number=1,
            url="https://github.test/org/repo/issues/1",
            title="broken screen",
            body="report",
        )
        context = TriageContext(
            issue=issue,
            comments=(),
            prd_hits=(),
            media=(
                MediaEvidence(
                    kind="image",
                    url="https://assets/x.png",
                    watermark=self.candidates_json,
                ),
            ),
        )
        markdown = render_triage_context_markdown(context)
        self.assertIn(
            "  - Watermark: [Watermark] 1 candidate envelope(s) (encrypted)",
            markdown,
        )

    def test_render_triage_context_markdown_renders_not_found_note(self) -> None:
        issue = GitHubIssue(
            number=2,
            url="https://github.test/org/repo/issues/2",
            title="clean screen",
            body="report",
        )
        context = TriageContext(
            issue=issue,
            comments=(),
            prd_hits=(),
            media=(
                MediaEvidence(
                    kind="image",
                    url="https://assets/x.png",
                    watermark=NO_WATERMARK_NOTE,
                ),
            ),
        )
        markdown = render_triage_context_markdown(context)
        self.assertIn(f"  - Watermark: {NO_WATERMARK_NOTE}", markdown)


class RunnerWatermarkDecryptTest(unittest.TestCase):
    """The triage runner decrypts candidate envelopes with the GH Actions key."""

    private_pem: str
    public_pem: str
    candidates_json: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.private_pem, cls.public_pem = _keypair()
        cls.envelope = build_envelope(
            _payload(),
            public_key_pem=cls.public_pem,
            key_id=DEFAULT_KEY_ID,
        )
        envelope_bytes = json.dumps(cls.envelope, separators=(",", ":")).encode("utf-8")
        cls.candidates_json = candidates_to_compact_json([envelope_bytes])

    def _store(self) -> WatermarkKeyStore:
        return WatermarkKeyStore(keys={DEFAULT_KEY_ID: self.private_pem})

    def _media(self, watermark: str) -> tuple[MediaEvidence, ...]:
        return (MediaEvidence(kind="image", url="https://assets/x.png", watermark=watermark),)

    def test_decrypts_clean_candidate_to_payload(self) -> None:
        media = resolve_media_watermarks(self._media(self.candidates_json), key_store=self._store())
        self.assertEqual(media[0].watermark, payload_to_compact_json(_payload()))

    def test_no_key_is_a_noop_keeping_candidates(self) -> None:
        media = resolve_media_watermarks(self._media(self.candidates_json), key_store=WatermarkKeyStore())
        self.assertEqual(media[0].watermark, self.candidates_json)

    def test_all_candidates_fail_reports_decrypt_failure(self) -> None:
        other_private, _ = _keypair()
        store = WatermarkKeyStore(keys={DEFAULT_KEY_ID: other_private})
        media = resolve_media_watermarks(self._media(self.candidates_json), key_store=store)
        self.assertEqual(media[0].watermark, watermark_failure_note(ERROR_DECRYPT))

    def test_unknown_key_id_reports_key_not_found(self) -> None:
        other_private, other_public = _keypair()
        envelope = build_envelope(
            _payload(keyId="retired-key-v0"),
            public_key_pem=other_public,
            key_id="retired-key-v0",
        )
        envelope_bytes = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        candidates = candidates_to_compact_json([envelope_bytes])
        store = WatermarkKeyStore(keys={DEFAULT_KEY_ID: other_private})
        media = resolve_media_watermarks(self._media(candidates), key_store=store)
        self.assertEqual(media[0].watermark, watermark_failure_note(ERROR_KEY_UNKNOWN))

    def test_picks_the_clean_candidate_over_a_corrupt_one(self) -> None:
        # A wrong-read ±1px offset can look like a valid envelope but carry
        # corrupted ciphertext. GCM auth picks the clean candidate, so a list
        # [corrupt, clean] must resolve to the clean payload.
        corrupt = dict(self.envelope)
        data = dict(corrupt["data"])
        assert isinstance(data, dict)
        ciphertext = data["ciphertext"]
        assert isinstance(ciphertext, str)
        data["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        corrupt["data"] = data
        both = candidates_to_compact_json(
            [
                json.dumps(corrupt, separators=(",", ":")).encode("utf-8"),
                json.dumps(self.envelope, separators=(",", ":")).encode("utf-8"),
            ]
        )
        media = resolve_media_watermarks(self._media(both), key_store=self._store())
        self.assertEqual(media[0].watermark, payload_to_compact_json(_payload()))

    def test_non_candidate_watermark_is_left_untouched(self) -> None:
        for value in (NO_WATERMARK_NOTE, payload_to_compact_json(_payload()), "not json"):
            media = resolve_media_watermarks(self._media(value), key_store=self._store())
            self.assertEqual(media[0].watermark, value)

    def test_build_triage_context_decrypts_with_env_key(self) -> None:
        issue = GitHubIssue(
            number=3,
            url="https://github.test/org/repo/issues/3",
            title="env-key decrypt",
            body=(
                "- image: https://assets/x.png\n"
                f"  - watermark-candidates: {self.candidates_json}\n"
            ),
        )
        with patch.dict(
            os.environ,
            {ENV_PRIVATE_KEY: self.private_pem, ENV_KEYS_JSON: ""},
            clear=True,
        ):
            context = build_triage_context(
                issue=issue,
                prd_root=Path(tempfile.mkdtemp()),
                prd_include_globs=("*.md",),
            )
        self.assertEqual(len(context.media), 1)
        self.assertEqual(context.media[0].watermark, payload_to_compact_json(_payload()))


class WatermarkCliTest(unittest.TestCase):
    """bugpatrol watermark decode --image <path> [--json]"""

    private_pem: str
    public_pem: str
    watermarked_png: bytes

    @classmethod
    def setUpClass(cls) -> None:
        cls.private_pem, cls.public_pem = _keypair()
        cls.watermarked_png = embed_envelope_trailer(
            _png_1x1(),
            build_envelope(
                _payload(),
                public_key_pem=cls.public_pem,
                key_id=DEFAULT_KEY_ID,
            ),
        )

    def _run(self, args: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(args)
        return exit_code, stdout.getvalue()

    def test_json_found_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            path.write_bytes(self.watermarked_png)
            with patch.dict(os.environ, {ENV_PRIVATE_KEY: self.private_pem, ENV_KEYS_JSON: ""}, clear=True):
                exit_code, out = self._run(["watermark", "decode", "--image", str(path), "--json"])
        self.assertEqual(exit_code, 0)
        parsed = json.loads(out)
        self.assertTrue(parsed["found"])
        self.assertEqual(parsed["confidence"], 1.0)
        self.assertEqual(parsed["keyId"], DEFAULT_KEY_ID)
        self.assertEqual(parsed["payload"]["watermarkId"], "wm-abc123")

    def test_json_not_found_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clean.png"
            path.write_bytes(_png_1x1())
            with patch.dict(os.environ, {ENV_PRIVATE_KEY: self.private_pem, ENV_KEYS_JSON: ""}, clear=True):
                exit_code, out = self._run(["watermark", "decode", "--image", str(path), "--json"])
        self.assertEqual(exit_code, 0)
        parsed = json.loads(out)
        self.assertFalse(parsed["found"])
        self.assertEqual(parsed["error"], ERROR_NOT_FOUND)

    def test_missing_private_key_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            path.write_bytes(self.watermarked_png)
            with patch.dict(os.environ, {ENV_PRIVATE_KEY: "", ENV_KEYS_JSON: ""}, clear=True):
                exit_code, out = self._run(["watermark", "decode", "--image", str(path), "--json"])
        self.assertEqual(exit_code, 1)
        parsed = json.loads(out)
        self.assertEqual(parsed["error"], ERROR_KEY_MISSING)

    def test_missing_image_exits_two(self) -> None:
        exit_code, out = self._run(
            ["watermark", "decode", "--image", "/nonexistent/shot.png", "--json"]
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(out)["error"], "watermark_image_not_found")

    def test_human_output_without_json_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            path.write_bytes(self.watermarked_png)
            with patch.dict(os.environ, {ENV_PRIVATE_KEY: self.private_pem, ENV_KEYS_JSON: ""}, clear=True):
                exit_code, out = self._run(["watermark", "decode", "--image", str(path)])
        self.assertEqual(exit_code, 0)
        self.assertIn("watermark found", out)
        self.assertIn("[Watermark]", out)


if __name__ == "__main__":
    unittest.main()
