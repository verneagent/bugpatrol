from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bugpatrol.backfill import (
    attachments_from_lark_message,
    intake_record_from_lark_message,
    run_lark_backfill,
    should_skip_message,
)
from bugpatrol.config import load_project_config
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.ledger import JsonMessageLedger
from bugpatrol.lark import DownloadedLarkResource, LarkMessage
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient


def message(**overrides: object) -> LarkMessage:
    values = {
        "message_id": "om_1",
        "chat_id": "oc_d371f022f168b567a141ced142691894",
        "root_id": "om_root",
        "sender_open_id": "ou_user",
        "sender_type": "user",
        "create_time": "2026-06-30T14:00:00Z",
        "msg_type": "text",
        "text": "Todo 删除最后一项后空状态没出现",
    }
    values.update(overrides)
    return LarkMessage(**values)  # type: ignore[arg-type]


class FakeLarkHistory(FakeLarkMessengerClient):
    def __init__(self, messages: list[LarkMessage]) -> None:
        super().__init__()
        self._messages = messages
        self.downloads: list[tuple[str, str, str]] = []

    def list_chat_messages(self, *, chat_id: str, limit: int = 20) -> list[LarkMessage]:
        return self._messages[:limit]

    def download_message_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        resource_type: str = "",
    ) -> DownloadedLarkResource:
        self.downloads.append((message_id, resource_key, resource_type))
        return DownloadedLarkResource(
            content=b"image-bytes",
            content_type="image/png",
            filename="bug.png",
        )


class FakeResourceStore:
    def __init__(self, url: str) -> None:
        self.url = url
        self.writes: list[tuple[str, str, str]] = []

    def write(self, *, ref, resource) -> str:  # type: ignore[no-untyped-def]
        self.writes.append((ref.message_id, ref.resource_key, resource.filename))
        return self.url


class FakeResourceRedactor:
    def redact(self, *, ref, resource):  # type: ignore[no-untyped-def]
        return DownloadedLarkResource(
            content=b"redacted",
            content_type=resource.content_type,
            filename="redacted.png",
        )


class FakeResourceTransformer:
    def transform(self, *, ref, resource):  # type: ignore[no-untyped-def]
        return DownloadedLarkResource(
            content=b"resized",
            content_type=resource.content_type,
            filename="resized.png",
        )


class BackfillTest(unittest.TestCase):
    def test_should_skip_bot_and_backlink_messages(self) -> None:
        self.assertTrue(should_skip_message(message(sender_open_id="ou_bot"), bot_open_id="ou_bot"))
        self.assertTrue(
            should_skip_message(
                message(sender_open_id="", sender_type="app", sender_id="cli_bugpatrol", sender_id_type="app_id"),
                bot_open_id="ou_bot",
                bot_app_id="cli_bugpatrol",
            )
        )
        self.assertFalse(
            should_skip_message(
                message(sender_open_id="", sender_type="app", sender_id="cli_reporter", sender_id_type="app_id"),
                bot_open_id="ou_bot",
                bot_app_id="cli_bugpatrol",
            )
        )
        self.assertTrue(
            should_skip_message(
                message(text="已创建 GitHub issue #1: https://github.com/x/y/issues/1"),
                bot_open_id="ou_bot",
            )
        )
        self.assertTrue(should_skip_message(message(msg_type="image", text=""), bot_open_id="ou_bot"))
        self.assertFalse(should_skip_message(message(), bot_open_id="ou_bot"))

    def test_should_skip_lark_system_messages(self) -> None:
        self.assertTrue(
            should_skip_message(
                message(
                    sender_open_id="",
                    text='{"template":"{from_user} created a topic group."}',
                    raw_content=json.dumps({"template": "{from_user} created a topic group."}),
                ),
                bot_open_id="ou_bot",
            )
        )
        self.assertTrue(
            should_skip_message(
                message(
                    text='{"template":"{from_user} created a topic group."}',
                    raw_content=json.dumps({"template": "{from_user} created a topic group."}),
                ),
                bot_open_id="ou_bot",
            )
        )
        self.assertTrue(should_skip_message(message(msg_type="system"), bot_open_id="ou_bot"))

    def test_intake_record_from_lark_message_maps_topic_root(self) -> None:
        record = intake_record_from_lark_message(
            message(),
            message_url_template="https://applink.larksuite.com/client/chat/open?openChatId={chat_id}&messageId={message_id}",
        )

        self.assertEqual(record.root_id, "om_root")
        self.assertEqual(record.message_id, "om_1")
        self.assertEqual(record.original_text, "Todo 删除最后一项后空状态没出现")
        self.assertEqual(
            record.lark_topic_url,
            "https://applink.larksuite.com/client/chat/open?openChatId=oc_d371f022f168b567a141ced142691894&messageId=om_root",
        )
        self.assertEqual(
            record.lark_message_url,
            "https://applink.larksuite.com/client/chat/open?openChatId=oc_d371f022f168b567a141ced142691894&messageId=om_1",
        )

    def test_intake_record_from_lark_app_message_uses_app_reporter(self) -> None:
        record = intake_record_from_lark_message(
            message(sender_open_id="", sender_type="app", sender_id="cli_reporter", sender_id_type="app_id")
        )

        self.assertEqual(record.reporter_name, "Lark app")
        self.assertEqual(record.reporter_open_id, "cli_reporter")

    def test_intake_record_from_lark_app_message_uses_configured_sender_name(self) -> None:
        record = intake_record_from_lark_message(
            message(sender_open_id="", sender_type="app", sender_id="cli_reporter", sender_id_type="app_id"),
            sender_names={"cli_reporter": "Reporter Bot"},
        )

        self.assertEqual(record.reporter_name, "Reporter Bot (Lark app)")
        self.assertEqual(record.reporter_open_id, "cli_reporter")

    def test_image_message_extracts_attachment(self) -> None:
        msg = message(
            msg_type="image",
            text="",
            raw_content=json.dumps({"image_key": "img_v2_abc"}),
        )

        attachments = attachments_from_lark_message(msg)
        record = intake_record_from_lark_message(msg)

        self.assertFalse(should_skip_message(msg, bot_open_id="ou_bot"))
        self.assertEqual(attachments[0].kind, "image")
        self.assertEqual(attachments[0].url, "lark://message/om_1/image/img_v2_abc")
        self.assertEqual(record.attachments, attachments)

    def test_file_message_extracts_name_description(self) -> None:
        msg = message(
            msg_type="file",
            text="",
            raw_content=json.dumps({"file_key": "file_v2_abc", "file_name": "console.log"}),
        )

        attachments = attachments_from_lark_message(msg)

        self.assertEqual(attachments[0].kind, "file")
        self.assertEqual(attachments[0].description, "console.log")

    def test_post_message_with_missing_open_id_is_user_intake(self) -> None:
        msg = message(
            sender_open_id="",
            sender_type="user",
            msg_type="post",
            text="拨打之前在手机上登录过的账号，会收到通话邀请",
            raw_content=json.dumps(
                {
                    "title": "",
                    "content": [
                        [
                            {
                                "tag": "media",
                                "file_key": "file_v3_repro",
                                "image_key": "img_v3_thumb",
                            }
                        ],
                        [{"tag": "text", "text": "拨打之前在手机上登录过的账号，会收到通话邀请"}],
                    ],
                },
                ensure_ascii=False,
            ),
        )

        attachments = attachments_from_lark_message(msg)

        self.assertFalse(should_skip_message(msg, bot_open_id="ou_bot"))
        self.assertEqual(attachments[0].kind, "video")
        self.assertEqual(attachments[0].url, "lark://message/om_1/media/file_v3_repro")

    def test_run_lark_backfill_processes_non_bot_messages_oldest_first(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                message(message_id="om_new", root_id="om_root", text="补充信息"),
                message(message_id="om_old", root_id="om_root", text="首次上报"),
                message(message_id="om_bot", sender_open_id=config.lark.bot_open_id, text="bot reply"),
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(config=config, lark=lark, workflow=workflow, limit=10)

        self.assertEqual(result.scanned, 3)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.processed, 2)
        self.assertEqual(result.outcomes[0].action, "created")
        self.assertEqual(result.outcomes[1].action, "updated")
        self.assertEqual(
            [(event.message_id, event.action, event.reason) for event in result.events],
            [
                ("om_bot", "skipped", "bot_message"),
                ("om_old", "processed", "created"),
                ("om_new", "processed", "updated"),
            ],
        )
        self.assertIn("首次上报", github.created[0].issue.body)
        self.assertIn("补充信息", github.created[0].comments[0])

    def test_run_lark_backfill_creates_issue_for_image_message(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                message(
                    msg_type="image",
                    text="",
                    raw_content=json.dumps({"image_key": "img_v2_abc"}),
                )
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(config=config, lark=lark, workflow=workflow, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(github.created[0].fields["Evidence"], "截图")
        self.assertIn("lark://message/om_1/image/img_v2_abc", github.created[0].issue.body)

    def test_run_lark_backfill_materializes_resources_when_configured(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                message(
                    msg_type="image",
                    text="",
                    raw_content=json.dumps({"image_key": "img_v2_abc"}),
                )
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        with tempfile.TemporaryDirectory() as tmp:
            result = run_lark_backfill(
                config=config,
                lark=lark,
                workflow=workflow,
                limit=10,
                resource_dir=Path(tmp),
            )
            resource_path = Path(tmp) / "om_1" / "bug.png"
            self.assertTrue(resource_path.exists())
            self.assertIn(str(resource_path), github.created[0].issue.body)

        self.assertEqual(result.processed, 1)
        self.assertEqual(lark.downloads, [("om_1", "img_v2_abc", "image")])

    def test_run_lark_backfill_materializes_resources_to_asset_repo(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                message(
                    msg_type="image",
                    text="",
                    raw_content=json.dumps({"image_key": "img_v2_abc"}),
                )
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        store = FakeResourceStore(
            "https://github.com/example-org/example-assets/raw/main/.github/issue-assets/om_1/bug.png"
        )

        result = run_lark_backfill(
            config=config,
            lark=lark,
            workflow=workflow,
            limit=10,
            resource_store=store,
        )

        self.assertEqual(result.processed, 1)
        self.assertEqual(lark.downloads, [("om_1", "img_v2_abc", "image")])
        self.assertEqual(store.writes, [("om_1", "img_v2_abc", "bug.png")])
        self.assertIn(store.url, github.created[0].issue.body)
        self.assertNotIn("lark://message/om_1/image/img_v2_abc", github.created[0].issue.body)

    def test_run_lark_backfill_applies_redactor_before_store(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                message(
                    msg_type="image",
                    text="",
                    raw_content=json.dumps({"image_key": "img_v2_abc"}),
                )
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        store = FakeResourceStore("https://assets/redacted.png")

        result = run_lark_backfill(
            config=config,
            lark=lark,
            workflow=workflow,
            limit=10,
            resource_store=store,
            resource_redactor=FakeResourceRedactor(),
        )

        self.assertEqual(result.processed, 1)
        self.assertEqual(store.writes, [("om_1", "img_v2_abc", "redacted.png")])

    def test_run_lark_backfill_applies_transformer_before_store(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                message(
                    msg_type="image",
                    text="",
                    raw_content=json.dumps({"image_key": "img_v2_abc"}),
                )
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        store = FakeResourceStore("https://assets/resized.png")

        result = run_lark_backfill(
            config=config,
            lark=lark,
            workflow=workflow,
            limit=10,
            resource_store=store,
            resource_transformer=FakeResourceTransformer(),
        )

        self.assertEqual(result.processed, 1)
        self.assertEqual(store.writes, [("om_1", "img_v2_abc", "resized.png")])

    def test_run_lark_backfill_rejects_two_resource_stores(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory([message()])
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                run_lark_backfill(
                    config=config,
                    lark=lark,
                    workflow=workflow,
                    resource_dir=Path(tmp),
                    resource_store=FakeResourceStore("https://assets/bug.png"),
                )

    def test_dry_run_does_not_write(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory([message()])
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(config=config, lark=lark, workflow=workflow, dry_run=True)

        self.assertEqual(result.processed, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.events[0].reason, "dry_run")
        self.assertEqual(github.created, [])

    def test_processed_ledger_skips_previously_processed_messages(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory([message(message_id="om_seen"), message(message_id="om_new")])
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonMessageLedger.load(Path(tmp) / "processed.json")
            ledger.mark_processed("om_seen")
            result = run_lark_backfill(
                config=config,
                lark=lark,
                workflow=workflow,
                processed_ledger=ledger,
            )

            loaded = JsonMessageLedger.load(Path(tmp) / "processed.json")

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.skipped, 1)
        self.assertTrue(loaded.is_processed("om_seen"))
        self.assertTrue(loaded.is_processed("om_new"))

    def test_dry_run_does_not_mark_processed_ledger(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory([message(message_id="om_dry")])
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonMessageLedger.load(Path(tmp) / "processed.json")
            result = run_lark_backfill(
                config=config,
                lark=lark,
                workflow=workflow,
                dry_run=True,
                processed_ledger=ledger,
            )

        self.assertEqual(result.processed, 0)
        self.assertFalse(ledger.is_processed("om_dry"))

    def test_dry_run_does_not_materialize_resources(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                message(
                    msg_type="image",
                    text="",
                    raw_content=json.dumps({"image_key": "img_v2_abc"}),
                )
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        with tempfile.TemporaryDirectory() as tmp:
            result = run_lark_backfill(
                config=config,
                lark=lark,
                workflow=workflow,
                dry_run=True,
                resource_dir=Path(tmp),
            )

        self.assertEqual(result.processed, 0)
        self.assertEqual(lark.downloads, [])


if __name__ == "__main__":
    unittest.main()
