"""Watermark decode: deterministic plaintext extraction and pipeline wiring.

Covers the required failure modes:

- no watermark image        -> found:false, watermark_not_found
- valid fixture image       -> decoded plaintext payload (all core fields)
- corrupted carrier         -> fails visibly (watermark_invalid_envelope)
- RS within budget          -> corrected, still decodes
- RS beyond budget          -> watermark_not_found (deterministic no-op)

plus the triage-pipeline integration (materialize -> intake render ->
media-evidence extraction -> triage context) and the CLI contract.

The payload is PLAINTEXT (no encryption): there are no keys, no GH secrets,
no decryption step anywhere in the pipeline.
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from PIL import Image

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
)
from bugpatrol.watermark import (
    ERROR_BAD_ENVELOPE,
    ERROR_NOT_FOUND,
    NO_WATERMARK_NOTE,
    decode_image,
    embed_payload_png_text,
    embed_payload_trailer,
    embed_screenshot_payload,
    payload_to_compact_json,
    render_payload_summary,
    watermark_failure_note,
)
from bugpatrol.watermark.extractor import (
    PAIR_COUNT,
    PAIR_OFFSET,
    RS_ENCODED_BYTES,
    WatermarkInvalidEnvelope,
    WM_MAX_PAYLOAD_BYTES,
    extract_plaintext_payload,
    gen_centers,
)


def _payload(**overrides: object) -> dict[str, object]:
    """Dev-mode payload: the 8 core fields plus the dev-only ``uid``."""
    base: dict[str, object] = {
        "schemaVersion": 2,
        "appVersion": "1.2.3",
        "buildVersion": "42",
        "buildTime": "2026-08-25T00:00:00Z",
        "modelName": "iPhone 15",
        "osName": "iOS",
        "osVersion": "18.5",
        "uid": "u_12345",
        "capturedAt": "2026-08-25T08:00:00Z",
    }
    base.update(overrides)
    return base


def _prod_payload() -> dict[str, object]:
    """Production payload: no ``uid`` key at all (dev-only field is omitted)."""
    payload = _payload()
    del payload["uid"]
    return payload


def _png_1x1() -> bytes:
    """A minimal but structurally valid 1x1 RGBA PNG."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    idat = _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
    iend = _png_chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def _png_canvas(width: int = 1080, height: int = 2340) -> bytes:
    """A plain screenshot-like PNG at the nominal canvas size (uniform bg)."""
    image = Image.new("RGB", (width, height), (142, 137, 129))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return length + chunk_type + data + struct.pack(">I", crc)


def _invert_encoded_byte(image: Image.Image, encoded_pos: int) -> None:
    """Swap every chip pair carrying one encoded byte, inverting its value.

    Stages byte errors for the RS-budget tests: flipping all PAIR_COUNT pairs of
    all 8 bits guarantees the majority vote flips, so each call is exactly one
    corrupted byte in the carrier's RS(255,135) codeword.
    """
    centers = gen_centers(RS_ENCODED_BYTES * 8 * PAIR_COUNT)
    for bit in range(8):
        bit_index = encoded_pos * 8 + bit
        for pair in range(PAIR_COUNT):
            cx, cy = centers[bit_index * PAIR_COUNT + pair]
            ax, ay = cx - PAIR_OFFSET, cy
            bx, by = cx + PAIR_OFFSET, cy
            left = image.crop((ax - 1, ay - 1, ax + 2, ay + 2))
            right = image.crop((bx - 1, by - 1, bx + 2, by + 2))
            image.paste(right, (ax - 1, ay - 1))
            image.paste(left, (bx - 1, by - 1))


class WatermarkCarrierTest(unittest.TestCase):
    """Required failure modes + round-trip through the three carriers."""

    def test_no_watermark_returns_not_found(self) -> None:
        result = decode_image(_png_1x1())
        self.assertFalse(result.found)
        self.assertEqual(result.confidence, 0)
        self.assertEqual(result.error, ERROR_NOT_FOUND)

    def test_trailer_carrier_round_trips_full_payload(self) -> None:
        embedded = embed_payload_trailer(_png_1x1(), _payload())
        result = decode_image(embedded)
        self.assertTrue(result.found)
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.payload, _payload())

    def test_png_text_chunk_carrier_round_trips(self) -> None:
        embedded = embed_payload_png_text(_png_1x1(), _payload())
        result = decode_image(embedded)
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_screenshot_pixel_carrier_round_trips(self) -> None:
        embedded = embed_screenshot_payload(_png_canvas(), _payload())
        result = decode_image(embedded)
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_screenshot_pixel_carrier_round_trips_prod_payload_without_uid(self) -> None:
        embedded = embed_screenshot_payload(_png_canvas(), _prod_payload())
        result = decode_image(embedded)
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _prod_payload())
        self.assertNotIn("uid", result.payload or {})

    def test_pixel_carrier_works_at_half_nominal_scale(self) -> None:
        # A smaller native screen: the app scales the nominal 1080x2340 canvas
        # down and the extractor resizes back to 1080 wide. RS + majority vote
        # must survive the resample.
        embedded = embed_screenshot_payload(_png_canvas(width=540, height=1170), _payload())
        result = decode_image(embedded)
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_payload_over_capacity_raises(self) -> None:
        with self.assertRaises(ValueError):
            embed_screenshot_payload(_png_canvas(), _payload(filler="x" * WM_MAX_PAYLOAD_BYTES))

    def test_dev_and_prod_payloads_fit_carrier_budget(self) -> None:
        dev = payload_to_compact_json(_payload()).encode("utf-8")
        prod = payload_to_compact_json(_prod_payload()).encode("utf-8")
        self.assertLessEqual(len(dev), WM_MAX_PAYLOAD_BYTES)
        self.assertLessEqual(len(prod), WM_MAX_PAYLOAD_BYTES)

    def test_corrupt_trailer_base64_fails_visibly(self) -> None:
        bad = _png_1x1() + b"BUGPATROL_WM1:%%%not-base64%%%:BUGPATROL_WM1"
        result = decode_image(bad)
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_BAD_ENVELOPE)

    def test_truncated_trailer_fails_visibly(self) -> None:
        bad = _png_1x1() + b"BUGPATROL_WM1:aaaa"
        result = decode_image(bad)
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_BAD_ENVELOPE)

    def test_non_dict_payload_fails_visibly(self) -> None:
        # A carrier whose base64 decodes to a non-object JSON value is an
        # envelope contract violation, not a clean payload.
        embedded = _png_1x1() + b"BUGPATROL_WM1:" + _b64(b'"just a string"') + b":BUGPATROL_WM1"
        result = decode_image(embedded)
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_BAD_ENVELOPE)


class WatermarkRsBudgetTest(unittest.TestCase):
    """RS(255,135)x2 error correction on the pixel carrier."""

    def _corrupted_png(self, n_bytes: int) -> bytes:
        embedded = embed_screenshot_payload(_png_canvas(), _payload())
        image = Image.open(io.BytesIO(embedded)).convert("RGB")
        for pos in range(n_bytes):
            _invert_encoded_byte(image, pos)
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()

    def test_corrects_up_to_full_budget_per_block(self) -> None:
        # t = nsym/2 = 60: the first 60 bytes of block 0 may all be corrupted
        # and RS must still recover the payload.
        result = decode_image(self._corrupted_png(60))
        self.assertTrue(result.found)
        self.assertEqual(result.payload, _payload())

    def test_beyond_budget_returns_not_found(self) -> None:
        # 61 corrupted bytes in one block exceeds t=60 -> the block fails RS and
        # the read is a deterministic no-op (not a wrong payload).
        result = decode_image(self._corrupted_png(61))
        self.assertFalse(result.found)
        self.assertEqual(result.error, ERROR_NOT_FOUND)


class WatermarkPipelineTest(unittest.TestCase):
    """Plaintext payload rides through materialize, intake render, triage render."""

    watermarked_png: bytes
    compact: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.watermarked_png = embed_payload_trailer(_png_1x1(), _payload())
        cls.compact = payload_to_compact_json(_payload())

    def _watermarked_attachment(self) -> Attachment:
        return Attachment(
            kind="image",
            url="lark://message/om_wm/image/img_v2_wm",
        )

    def test_materialize_attachment_extracts_plaintext(self) -> None:
        class Downloader:
            def download_message_resource(self, **kwargs: object) -> DownloadedLarkResource:
                return DownloadedLarkResource(
                    content=WatermarkPipelineTest.watermarked_png,
                    content_type="image/png",
                    filename="bug screenshot.png",
                )

        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_attachment(
                attachment=self._watermarked_attachment(),
                lark=Downloader(),
                store=LocalResourceStore(Path(tmp)),
            )
        self.assertEqual(materialized.watermark, self.compact)

    def test_materialize_extracts_before_transform_strips_carrier(self) -> None:
        # A redactor that rewrites the bytes (as a JPEG re-encode would) must
        # not lose the watermark, because extraction runs on the ORIGINAL bytes
        # first.
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

        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_attachment(
                attachment=self._watermarked_attachment(),
                lark=Downloader(),
                store=LocalResourceStore(Path(tmp)),
                redactor=StrippingRedactor(),
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

    def test_materialize_non_media_is_not_attempted(self) -> None:
        # A document attachment is not a watermark candidate; the line stays ""
        # (never scanned), distinct from the "checked, absent" note.
        class Downloader:
            def download_message_resource(self, **kwargs: object) -> DownloadedLarkResource:
                return DownloadedLarkResource(
                    content=b"some pdf",
                    content_type="application/pdf",
                    filename="spec.pdf",
                )

        attachment = materialize_attachment(
            attachment=Attachment(
                kind="file",
                url="lark://message/om_wm/file/img_v2_wm",
            ),
            lark=Downloader(),
            store=LocalResourceStore(Path(tempfile.mkdtemp())),
        )
        self.assertEqual(attachment.watermark, "")

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

    def test_extract_media_evidence_parses_watermark_line(self) -> None:
        markdown = (
            "- image: https://assets/x.png\n"
            f"  - watermark: {self.compact}\n"
        )
        evidence = extract_media_evidence(markdown)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].watermark, self.compact)

    def test_render_triage_context_markdown_summarizes_payload(self) -> None:
        # The triage agent must see a readable key=value summary, not raw JSON.
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
        self.assertIn("  - Watermark: [Watermark] schemaVersion=2", markdown)
        self.assertIn("appVersion=1.2.3", markdown)
        self.assertIn("modelName=iPhone 15", markdown)

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

    def test_build_triage_context_parses_watermark_from_issue_body(self) -> None:
        issue = GitHubIssue(
            number=3,
            url="https://github.test/org/repo/issues/3",
            title="plaintext from body",
            body=(
                "- image: https://assets/x.png\n"
                f"  - watermark: {self.compact}\n"
            ),
        )
        context = build_triage_context(
            issue=issue,
            prd_root=Path(tempfile.mkdtemp()),
            prd_include_globs=("*.md",),
        )
        self.assertEqual(len(context.media), 1)
        self.assertEqual(context.media[0].watermark, self.compact)


class WatermarkCliTest(unittest.TestCase):
    """bugpatrol watermark decode --image <path> [--json]"""

    watermarked_png: bytes

    @classmethod
    def setUpClass(cls) -> None:
        cls.watermarked_png = embed_payload_trailer(_png_1x1(), _payload())

    def _run(self, args: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(args)
        return exit_code, stdout.getvalue()

    def test_json_found_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            path.write_bytes(self.watermarked_png)
            exit_code, out = self._run(["watermark", "decode", "--image", str(path), "--json"])
        self.assertEqual(exit_code, 0)
        parsed = json.loads(out)
        self.assertTrue(parsed["found"])
        self.assertEqual(parsed["confidence"], 1.0)
        self.assertNotIn("keyId", parsed)
        self.assertEqual(parsed["payload"]["schemaVersion"], 2)
        self.assertEqual(parsed["payload"]["uid"], "u_12345")

    def test_json_not_found_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clean.png"
            path.write_bytes(_png_1x1())
            exit_code, out = self._run(["watermark", "decode", "--image", str(path), "--json"])
        self.assertEqual(exit_code, 0)
        parsed = json.loads(out)
        self.assertFalse(parsed["found"])
        self.assertEqual(parsed["error"], ERROR_NOT_FOUND)

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
            exit_code, out = self._run(["watermark", "decode", "--image", str(path)])
        self.assertEqual(exit_code, 0)
        self.assertIn("watermark found", out)
        self.assertIn("[Watermark]", out)
        self.assertIn("schemaVersion=2", out)


def _b64(data: bytes) -> bytes:
    import base64

    return base64.b64encode(data)


class WatermarkPlaintextContractTest(unittest.TestCase):
    """The payload is plaintext JSON; helpers round-trip without any crypto."""

    def test_extract_plaintext_payload_returns_json_bytes(self) -> None:
        embedded = embed_payload_trailer(_png_1x1(), _payload())
        raw = extract_plaintext_payload(embedded)
        self.assertIsNotNone(raw)
        self.assertEqual(json.loads(raw.decode("utf-8")), _payload())

    def test_corrupt_carrier_raises_invalid_envelope(self) -> None:
        with self.assertRaises(WatermarkInvalidEnvelope):
            extract_plaintext_payload(_png_1x1() + b"BUGPATROL_WM1:%%%:BUGPATROL_WM1")

    def test_render_payload_summary_lists_core_fields(self) -> None:
        summary = render_payload_summary(_payload())
        for field in ("schemaVersion", "appVersion", "buildVersion", "modelName", "osName", "osVersion"):
            self.assertIn(f"{field}=", summary)

    def test_watermark_failure_note(self) -> None:
        self.assertEqual(watermark_failure_note(ERROR_BAD_ENVELOPE), "水印解码失败 (watermark_invalid_envelope)")


if __name__ == "__main__":
    unittest.main()
