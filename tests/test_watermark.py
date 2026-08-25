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

from bugpatrol.__main__ import configured_watermark_decoder, main
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
    extract_media_evidence,
    render_triage_context_markdown,
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
    WatermarkKeyStore,
    WatermarkResourceDecoder,
    build_envelope,
    decode_image,
    embed_envelope_trailer,
    embed_png_text_envelope,
    embed_screenshot_pixel_envelope,
    payload_to_compact_json,
    watermark_failure_note,
)
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
    """Decoded metadata rides through materialization and triage rendering."""

    private_pem: str
    public_pem: str
    watermarked_png: bytes
    compact: str

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
        cls.compact = payload_to_compact_json(_payload())

    def _decoder(self) -> object:
        from bugpatrol.watermark import WatermarkResourceDecoder

        return WatermarkResourceDecoder(
            key_store=WatermarkKeyStore(keys={DEFAULT_KEY_ID: self.private_pem})
        )

    def _watermarked_attachment(self) -> Attachment:
        return Attachment(
            kind="image",
            url="lark://message/om_wm/image/img_v2_wm",
        )

    def test_materialize_attachment_decodes_watermark(self) -> None:
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
                watermark_decoder=self._decoder(),  # type: ignore[arg-type]
            )
        self.assertEqual(materialized.watermark, self.compact)

    def test_materialize_decodes_before_transform_strips_carrier(self) -> None:
        # A redactor that rewrites the bytes (as a JPEG re-encode would) must not
        # lose the watermark, because decode runs on the ORIGINAL bytes first.
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
                watermark_decoder=self._decoder(),  # type: ignore[arg-type]
            )
        self.assertEqual(materialized.watermark, self.compact)

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
            watermark_decoder=self._decoder(),  # type: ignore[arg-type]
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
            watermark_decoder=self._decoder(),  # type: ignore[arg-type]
        )
        self.assertEqual(attachment.watermark, NO_WATERMARK_NOTE)

    def test_materialize_without_decoder_is_silent(self) -> None:
        # Feature off (no decoder wired) must not fabricate a note: the
        # watermark line stays absent entirely.
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
        self.assertEqual(attachment.watermark, "")

    def test_materialize_reports_watermark_decode_failure(self) -> None:
        # An envelope that cannot be decrypted with the configured key must
        # surface a visible failure note, not silently drop the watermark line.
        class Downloader:
            def download_message_resource(self, **kwargs: object) -> DownloadedLarkResource:
                return DownloadedLarkResource(
                    content=WatermarkPipelineTest.watermarked_png,
                    content_type="image/png",
                    filename="bug screenshot.png",
                )

        other_private, _other_public = _keypair()
        wrong_decoder = WatermarkResourceDecoder(
            key_store=WatermarkKeyStore(keys={DEFAULT_KEY_ID: other_private})
        )
        attachment = materialize_attachment(
            attachment=self._watermarked_attachment(),
            lark=Downloader(),
            store=LocalResourceStore(Path(tempfile.mkdtemp())),
            watermark_decoder=wrong_decoder,
        )
        self.assertEqual(attachment.watermark, watermark_failure_note(ERROR_DECRYPT))

    def test_render_attachments_markdown_emits_watermark_line(self) -> None:
        copy = {
            "open_asset": "open asset",
            "preview": "preview",
            "image_alt": "image",
            "generated_description": "generated description",
            "none": "none",
        }
        markdown = render_attachments_markdown(
            (Attachment(kind="image", url="https://assets/x.png", watermark=self.compact),),
            copy=copy,
        )
        self.assertIn(f"- watermark: {self.compact}", markdown)

    def test_extract_media_evidence_parses_watermark_line(self) -> None:
        markdown = (
            "- image: https://assets/x.png\n"
            f"  - watermark: {self.compact}\n"
        )
        evidence = extract_media_evidence(markdown)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].watermark, self.compact)

    def test_render_triage_context_markdown_includes_watermark_summary(self) -> None:
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
                    watermark=self.compact,
                ),
            ),
        )
        markdown = render_triage_context_markdown(context)
        self.assertIn(
            "  - Watermark: [Watermark] keyId=diagnostic-watermark-v1 "
            "watermarkId=wm-abc123 uid=u_12345 pathname=/settings/account",
            markdown,
        )

    def test_intake_record_from_dict_round_trips_watermark(self) -> None:
        record = IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-08-25T00:00:00Z",
            chat_id="oc_1",
            root_id="om_1",
            message_id="om_1",
            original_text="bug",
            attachments=(Attachment(kind="image", url="u", watermark=self.compact),),
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
        self.assertEqual(restored.attachments[0].watermark, self.compact)

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

    def test_extract_media_evidence_parses_not_found_note(self) -> None:
        markdown = (
            "- image: https://assets/x.png\n"
            f"  - watermark: {NO_WATERMARK_NOTE}\n"
        )
        evidence = extract_media_evidence(markdown)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].watermark, NO_WATERMARK_NOTE)

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

    def test_configured_decoder_respects_env(self) -> None:
        with patch.dict(os.environ, {ENV_PRIVATE_KEY: "", ENV_KEYS_JSON: ""}, clear=True):
            self.assertIsNone(configured_watermark_decoder())
        with patch.dict(os.environ, {ENV_PRIVATE_KEY: self.private_pem, ENV_KEYS_JSON: ""}, clear=True):
            self.assertIsNotNone(configured_watermark_decoder())


if __name__ == "__main__":
    unittest.main()
