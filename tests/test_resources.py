from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.lark import DownloadedLarkResource
from bugpatrol.resources import (
    LocalResourceStore,
    materialize_lark_attachments,
    parse_lark_resource_url,
)


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def download_message_resource(self, *, message_id: str, resource_key: str) -> DownloadedLarkResource:
        self.calls.append((message_id, resource_key))
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

        self.assertEqual(downloader.calls, [("om_1", "img_v2_abc")])
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


if __name__ == "__main__":
    unittest.main()
