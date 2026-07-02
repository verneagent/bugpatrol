from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.lark import DownloadedLarkResource
from bugpatrol.resources import (
    GitHubAssetRepoStore,
    LocalResourceStore,
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
            checkout = Path(tmp) / "fived-assets"
            (checkout / ".git").mkdir(parents=True)
            run = Mock()
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""

            with patch("subprocess.run", run):
                ref = parse_lark_resource_url("lark://message/om_1/image/img_v2_abc")
                assert ref is not None
                url = GitHubAssetRepoStore(
                    repo="TheCloverLab/fived-assets",
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
                "https://github.com/TheCloverLab/fived-assets/raw/main/.github/issue-assets/om_1/bug_screenshot.png",
            )
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0], ["git", "-C", str(checkout), "pull", "--quiet", "origin", "main"])
            self.assertEqual(commands[1], ["git", "-C", str(checkout), "add", ".github/issue-assets/om_1/bug_screenshot.png"])
            self.assertEqual(commands[2][:5], ["git", "-C", str(checkout), "commit", "--no-verify"])
            self.assertEqual(commands[3], ["git", "-C", str(checkout), "push", "--no-verify", "origin", "main"])


if __name__ == "__main__":
    unittest.main()
