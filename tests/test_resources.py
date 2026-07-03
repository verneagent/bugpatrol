from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.lark import DownloadedLarkResource
from bugpatrol.resources import (
    CommandVideoFrameExtractor,
    CommandResourceDescriber,
    CommandResourceRedactor,
    CompositeResourceTransformer,
    FfprobeVideoDurationProbe,
    GitHubAssetRepoStore,
    ImageResourceResizer,
    LocalResourceStore,
    ResourcePolicy,
    materialize_attachment,
    materialize_lark_attachments,
    parse_lark_resource_url,
)


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def download_message_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        resource_type: str = "",
    ) -> DownloadedLarkResource:
        self.calls.append((message_id, resource_key, resource_type))
        return DownloadedLarkResource(
            content=b"image-bytes",
            content_type="image/png",
            filename="bug screenshot.png",
        )


class FakeVideoDownloader(FakeDownloader):
    def download_message_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        resource_type: str = "",
    ) -> DownloadedLarkResource:
        self.calls.append((message_id, resource_key, resource_type))
        return DownloadedLarkResource(
            content=b"video-bytes",
            content_type="video/mp4",
            filename="repro.mp4",
        )


class FakeVideoDurationProbe:
    def __init__(self, duration: float) -> None:
        self.duration = duration

    def duration_seconds(self, *, ref, resource) -> float:  # type: ignore[no-untyped-def]
        return self.duration


def png_bytes(*, width: int, height: int) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), color=(255, 0, 0))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class ResourcesTest(unittest.TestCase):
    def test_parse_lark_resource_url(self) -> None:
        ref = parse_lark_resource_url("lark://message/om_1/image/img_v2_abc")

        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.message_id, "om_1")
        self.assertEqual(ref.kind, "image")
        self.assertEqual(ref.resource_key, "img_v2_abc")

    def test_materialize_lark_attachments_writes_local_files(self) -> None:
        record = IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id="oc_1",
            root_id="om_1",
            message_id="om_1",
            original_text="bug",
            attachments=(Attachment(kind="image", url="lark://message/om_1/image/img_v2_abc"),),
        )
        downloader = FakeDownloader()
        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_lark_attachments(
                record=record,
                lark=downloader,
                store=LocalResourceStore(Path(tmp)),
            )

            path = Path(materialized.attachments[0].url)
            self.assertEqual(path.read_bytes(), b"image-bytes")
            self.assertEqual(path.name, "bug_screenshot.png")

        self.assertEqual(downloader.calls, [("om_1", "img_v2_abc", "image")])
        self.assertEqual(materialized.attachments[0].description, "bug screenshot.png")

    def test_materialize_keeps_non_lark_urls(self) -> None:
        record = IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id="oc_1",
            root_id="om_1",
            message_id="om_1",
            original_text="bug",
            attachments=(Attachment(kind="image", url="https://assets/image.png"),),
        )

        materialized = materialize_lark_attachments(
            record=record,
            lark=FakeDownloader(),
            store=LocalResourceStore(Path("/tmp/unused")),
        )

        self.assertEqual(materialized.attachments, record.attachments)

    def test_github_asset_repo_store_writes_and_pushes_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "example-assets"
            (checkout / ".git").mkdir(parents=True)
            run = Mock()
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""

            with patch("subprocess.run", run):
                ref = parse_lark_resource_url("lark://message/om_1/image/img_v2_abc")
                assert ref is not None
                url = GitHubAssetRepoStore(
                    repo="example-org/example-assets",
                    checkout_path=checkout,
                ).write(
                    ref=ref,
                    resource=DownloadedLarkResource(
                        content=b"image-bytes",
                        content_type="image/png",
                        filename="bug screenshot.png",
                    ),
                )

            asset_path = checkout / ".github" / "issue-assets" / "om_1" / "bug_screenshot.png"
            self.assertEqual(asset_path.read_bytes(), b"image-bytes")
            self.assertEqual(
                url,
                "https://github.com/example-org/example-assets/raw/main/.github/issue-assets/om_1/bug_screenshot.png",
            )
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0], ["git", "-C", str(checkout), "pull", "--quiet", "origin", "main"])
            self.assertEqual(commands[1], ["git", "-C", str(checkout), "add", ".github/issue-assets/om_1/bug_screenshot.png"])
            self.assertEqual(commands[2][:5], ["git", "-C", str(checkout), "commit", "--no-verify"])
            self.assertEqual(commands[3], ["git", "-C", str(checkout), "push", "--no-verify", "origin", "main"])

    def test_github_asset_repo_store_adds_extension_from_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "example-assets"
            (checkout / ".git").mkdir(parents=True)
            run = Mock()
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            ref = parse_lark_resource_url("lark://message/om_1/image/img_v2_abc")
            assert ref is not None

            with patch("subprocess.run", run):
                url = GitHubAssetRepoStore(
                    repo="example-org/example-assets",
                    checkout_path=checkout,
                ).write(
                    ref=ref,
                    resource=DownloadedLarkResource(
                        content=b"image-bytes",
                        content_type="image/png",
                        filename="",
                    ),
                )

            self.assertTrue((checkout / ".github" / "issue-assets" / "om_1" / "img_v2_abc.png").exists())
            self.assertTrue(url.endswith("/img_v2_abc.png"))

    def test_materialize_uses_command_description(self) -> None:
        record = IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id="oc_1",
            root_id="om_1",
            message_id="om_1",
            original_text="bug",
            attachments=(Attachment(kind="image", url="lark://message/om_1/image/img_v2_abc"),),
        )
        downloader = FakeDownloader()
        describer = CommandResourceDescriber(
            command=(
                "python3",
                "-c",
                "from pathlib import Path; import sys; print('visual: ' + Path(sys.argv[1]).read_text())",
                "{path}",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_lark_attachments(
                record=record,
                lark=downloader,
                store=LocalResourceStore(Path(tmp)),
                describer=describer,
            )

        self.assertEqual(materialized.attachments[0].description, "visual: image-bytes")

    def test_materialize_redacts_before_store_and_description(self) -> None:
        record = IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id="oc_1",
            root_id="om_1",
            message_id="om_1",
            original_text="bug",
            attachments=(Attachment(kind="image", url="lark://message/om_1/image/img_v2_abc"),),
        )
        redactor = CommandResourceRedactor(
            command=(
                "python3",
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'redacted-image')",
                "{path}",
            )
        )
        describer = CommandResourceDescriber(
            command=(
                "python3",
                "-c",
                "from pathlib import Path; import sys; print('visual: ' + Path(sys.argv[1]).read_text())",
                "{path}",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_lark_attachments(
                record=record,
                lark=FakeDownloader(),
                store=LocalResourceStore(Path(tmp)),
                describer=describer,
                redactor=redactor,
            )

            path = Path(materialized.attachments[0].url)
            self.assertEqual(path.read_bytes(), b"redacted-image")
            self.assertEqual(materialized.attachments[0].description, "visual: redacted-image")

    def test_image_resizer_scales_before_store_and_description(self) -> None:
        class ImageDownloader(FakeDownloader):
            def download_message_resource(self, *, message_id: str, resource_key: str, resource_type: str = "") -> DownloadedLarkResource:
                self.calls.append((message_id, resource_key, resource_type))
                return DownloadedLarkResource(
                    content=png_bytes(width=80, height=40),
                    content_type="image/png",
                    filename="wide.png",
                )

        record = IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id="oc_1",
            root_id="om_1",
            message_id="om_1",
            original_text="bug",
            attachments=(Attachment(kind="image", url="lark://message/om_1/image/img_v2_abc"),),
        )
        describer = CommandResourceDescriber(
            command=(
                "python3",
                "-c",
                (
                    "from PIL import Image; import sys; "
                    "im=Image.open(sys.argv[1]); print('size: ' + str(im.width) + 'x' + str(im.height))"
                ),
                "{path}",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_lark_attachments(
                record=record,
                lark=ImageDownloader(),
                store=LocalResourceStore(Path(tmp)),
                describer=describer,
                transformer=ImageResourceResizer(max_width=20, max_height=20),
            )

            path = Path(materialized.attachments[0].url)
            from PIL import Image

            with Image.open(path) as stored:
                self.assertEqual((stored.width, stored.height), (20, 10))
            self.assertEqual(materialized.attachments[0].description, "size: 20x10")

    def test_materialize_skips_resource_when_policy_rejects_size(self) -> None:
        attachment = Attachment(kind="image", url="lark://message/om_1/image/img_v2_abc")
        downloader = FakeDownloader()
        store = Mock()
        describer = Mock()

        materialized = materialize_attachment(
            attachment=attachment,
            lark=downloader,
            store=store,
            describer=describer,
            policy=ResourcePolicy(max_image_bytes=1),
        )

        self.assertEqual(materialized.url, attachment.url)
        self.assertIn("resource skipped", materialized.description)
        store.write.assert_not_called()
        describer.describe.assert_not_called()

    def test_materialize_skips_video_when_policy_rejects_duration(self) -> None:
        attachment = Attachment(kind="video", url="lark://message/om_1/media/file_v2_abc")
        store = Mock()
        describer = Mock()

        materialized = materialize_attachment(
            attachment=attachment,
            lark=FakeVideoDownloader(),
            store=store,
            describer=describer,
            policy=ResourcePolicy(
                max_video_duration_seconds=10,
                video_duration_probe=FakeVideoDurationProbe(12.5),
            ),
        )

        self.assertEqual(materialized.url, attachment.url)
        self.assertIn("video duration is 12.5s", materialized.description)
        store.write.assert_not_called()
        describer.describe.assert_not_called()

    def test_materialize_keeps_video_when_duration_is_within_policy(self) -> None:
        attachment = Attachment(kind="video", url="lark://message/om_1/media/file_v2_abc")
        store = Mock()
        store.write.return_value = "https://assets/repro.mp4"

        materialized = materialize_attachment(
            attachment=attachment,
            lark=FakeVideoDownloader(),
            store=store,
            policy=ResourcePolicy(
                max_video_duration_seconds=10,
                video_duration_probe=FakeVideoDurationProbe(9.5),
            ),
        )

        self.assertEqual(materialized.url, "https://assets/repro.mp4")
        store.write.assert_called_once()

    def test_video_frame_extractor_replaces_video_before_store_and_description(self) -> None:
        record = IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id="oc_1",
            root_id="om_1",
            message_id="om_1",
            original_text="bug",
            attachments=(Attachment(kind="video", url="lark://message/om_1/media/file_v2_abc"),),
        )
        transformer = CommandVideoFrameExtractor(
            command=(
                "python3",
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'frames-image')",
                "{output_path}",
            )
        )
        describer = CommandResourceDescriber(
            command=(
                "python3",
                "-c",
                "from pathlib import Path; import sys; print('frames: ' + Path(sys.argv[1]).read_text())",
                "{path}",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_lark_attachments(
                record=record,
                lark=FakeVideoDownloader(),
                store=LocalResourceStore(Path(tmp)),
                describer=describer,
                transformer=transformer,
            )

            path = Path(materialized.attachments[0].url)
            self.assertEqual(path.name, "repro.frames.png")
            self.assertEqual(path.read_bytes(), b"frames-image")
            self.assertEqual(materialized.attachments[0].description, "frames: frames-image")

    def test_video_frame_extractor_respects_min_duration(self) -> None:
        ref = parse_lark_resource_url("lark://message/om_1/media/file_v2_abc")
        assert ref is not None
        resource = DownloadedLarkResource(
            content=b"video-bytes",
            content_type="video/mp4",
            filename="repro.mp4",
        )
        transformer = CommandVideoFrameExtractor(
            command=("python3", "-c", "raise SystemExit(9)"),
            min_duration_seconds=10,
            duration_probe=FakeVideoDurationProbe(2),
        )

        self.assertIs(transformer.transform(ref=ref, resource=resource), resource)

    def test_composite_transformer_runs_in_order(self) -> None:
        class AppendTransformer:
            def __init__(self, suffix: bytes) -> None:
                self.suffix = suffix

            def transform(self, *, ref, resource):  # type: ignore[no-untyped-def]
                return DownloadedLarkResource(
                    content=resource.content + self.suffix,
                    content_type=resource.content_type,
                    filename=resource.filename,
                )

        ref = parse_lark_resource_url("lark://message/om_1/image/img_v2_abc")
        assert ref is not None
        transformed = CompositeResourceTransformer(
            (AppendTransformer(b"-a"), AppendTransformer(b"-b"))
        ).transform(
            ref=ref,
            resource=DownloadedLarkResource(content=b"x", content_type="image/png", filename="bug.png"),
        )

        self.assertEqual(transformed.content, b"x-a-b")

    def test_ffprobe_video_duration_probe_parses_stdout(self) -> None:
        ref = parse_lark_resource_url("lark://message/om_1/media/file_v2_abc")
        assert ref is not None
        completed = Mock(returncode=0, stdout="12.345\n", stderr="")

        with patch("bugpatrol.resources.subprocess.run", return_value=completed) as run:
            duration = FfprobeVideoDurationProbe(command=("ffprobe", "{path}")).duration_seconds(
                ref=ref,
                resource=DownloadedLarkResource(
                    content=b"video-bytes",
                    content_type="video/mp4",
                    filename="repro.mp4",
                ),
            )

        self.assertEqual(duration, 12.345)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_redaction_failure_blocks_materialization(self) -> None:
        ref = parse_lark_resource_url("lark://message/om_1/image/img_v2_abc")
        assert ref is not None

        with self.assertRaisesRegex(RuntimeError, "redaction command failed"):
            CommandResourceRedactor(
                command=("python3", "-c", "import sys; print('secret detector failed', file=sys.stderr); raise SystemExit(2)")
            ).redact(
                ref=ref,
                resource=DownloadedLarkResource(content=b"x", content_type="image/png", filename="bug.png"),
            )

    def test_command_description_failure_is_non_blocking(self) -> None:
        ref = parse_lark_resource_url("lark://message/om_1/image/img_v2_abc")
        assert ref is not None
        description = CommandResourceDescriber(
            command=("python3", "-c", "import sys; print('bad', file=sys.stderr); raise SystemExit(3)")
        ).describe(
            ref=ref,
            resource=DownloadedLarkResource(content=b"x", content_type="image/png", filename="bug.png"),
        )

        self.assertIn("vision description unavailable", description)
        self.assertIn("bad", description)

    def test_command_description_retries_transient_failures(self) -> None:
        ref = parse_lark_resource_url("lark://message/om_1/image/img_v2_abc")
        assert ref is not None
        first = Mock(returncode=1, stdout="", stderr="temporary")
        second = Mock(returncode=0, stdout="visual ok\n", stderr="")

        with patch("bugpatrol.resources.subprocess.run", side_effect=[first, second]) as run:
            with patch("bugpatrol.resources.time.sleep") as sleep:
                description = CommandResourceDescriber(
                    command=("vision", "{path}"),
                    retries=1,
                    retry_backoff_seconds=0.5,
                ).describe(
                    ref=ref,
                    resource=DownloadedLarkResource(content=b"x", content_type="image/png", filename="bug.png"),
                )

        self.assertEqual(description, "visual ok")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
