from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from bugpatrol.backfill import (
    TopicBatch,
    attachments_from_lark_message,
    expand_merge_forward,
    intake_record_from_lark_message,
    process_topic_batch,
    run_lark_backfill,
    scan_topic_batches,
    should_skip_message,
    skip_reason,
)
from bugpatrol import backfill as backfill_module
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
        return [m for m in self._messages if m.chat_id == chat_id][:limit]

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


def raw_lark_item(
    *,
    message_id: str,
    msg_type: str,
    content: str,
    sender_id: str = "ou_user",
    chat_id: str = "oc_source",
) -> dict[str, object]:
    """A raw Lark message item as returned inside a merged-forward detail."""
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "msg_type": msg_type,
        "create_time": "1788322389929",
        "body": {"content": content},
        "sender": {"id": sender_id, "id_type": "open_id", "sender_type": "user"},
    }


def merged_forward_message(**overrides: object) -> LarkMessage:
    return message(
        message_id="om_env",
        root_id="om_env",
        sender_open_id="ou_irisy",
        msg_type="merge_forward",
        text="Merged and Forwarded Message",
        **overrides,
    )


class FakeLarkMergedForward(FakeLarkHistory):
    def __init__(
        self,
        messages: list[LarkMessage],
        forward_items: list[dict[str, object]],
        member_names: dict[str, str] | None = None,
    ) -> None:
        super().__init__(messages)
        self._forward_items = list(forward_items)
        self._member_names = dict(member_names or {})
        self.forward_fetches: list[str] = []

    def fetch_forwarded_messages(self, *, message_id: str) -> list[dict[str, object]]:
        self.forward_fetches.append(message_id)
        return list(self._forward_items)

    def list_chat_members(self, *, chat_id: str) -> dict[str, str]:
        return dict(self._member_names)


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
    def test_should_skip_withdrawn_messages(self) -> None:
        self.assertTrue(should_skip_message(message(deleted=True), bot_open_id="ou_bot"))
        self.assertEqual(skip_reason(message(deleted=True), bot_open_id="ou_bot"), "withdrawn_message")

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

        # Both messages of the topic coalesce into one atomic create.
        self.assertEqual(result.scanned, 3)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.processed, 1)
        self.assertEqual(len(result.outcomes), 1)
        self.assertEqual(result.outcomes[0].action, "created")
        self.assertEqual(
            [(event.message_id, event.action, event.reason) for event in result.events],
            [
                ("om_bot", "skipped", "bot_message"),
                ("om_old", "processed", "created"),
                ("om_new", "processed", "created"),
            ],
        )
        self.assertEqual(len(github.created), 1)
        self.assertEqual(github.created[0].comments, [])
        # Every message survives in the single issue body.
        self.assertIn("首次上报", github.created[0].issue.body)
        self.assertIn("补充信息", github.created[0].issue.body)
        # The triage signal carries every message id as material.
        self.assertEqual(
            set(result.outcomes[0].triage_signal.material_message_ids),
            {"om_old", "om_new"},
        )

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

    def test_since_cutoff_skips_messages_before_intake_since(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = dataclasses.replace(
            config,
            intake=dataclasses.replace(config.intake, since="2026-07-06T00:00:00+08:00"),
        )
        github = FakeGitHubIssuesClient()
        # 2026-07-05 12:00 +08:00 in epoch ms (before cutoff) vs 2026-07-06 12:00 +08:00 (after)
        lark = FakeLarkHistory(
            [
                message(message_id="om_new", root_id="om_new", create_time="1783310400000"),
                message(message_id="om_old", root_id="om_old", create_time="1783224000000"),
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(config=config, lark=lark, workflow=workflow, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(
            [(event.message_id, event.action, event.reason) for event in result.events],
            [
                ("om_old", "skipped", "before_intake_since"),
                ("om_new", "processed", "created"),
            ],
        )

    def test_since_cutoff_supports_iso_create_time(self) -> None:
        self.assertTrue(
            should_skip_message(
                message(create_time="2026-06-30T14:00:00Z"),
                bot_open_id="ou_bot",
                since_ms=1783267200000,  # 2026-07-06T00:00:00+08:00
            )
        )
        self.assertFalse(
            should_skip_message(
                message(create_time="2026-07-06T14:00:00+08:00"),
                bot_open_id="ou_bot",
                since_ms=1783267200000,
            )
        )

    def test_unparseable_create_time_is_not_skipped_by_since(self) -> None:
        self.assertFalse(
            should_skip_message(
                message(create_time="not-a-time"),
                bot_open_id="ou_bot",
                since_ms=1783267200000,
            )
        )

    def test_skip_orphan_replies_skips_replies_without_existing_issue(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = dataclasses.replace(
            config,
            intake=dataclasses.replace(config.intake, skip_orphan_replies=True),
        )
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                message(message_id="om_orphan_reply", root_id="om_pre_cutover", text="老话题里的补充"),
                message(message_id="om_root", root_id="om_root", text="新话题首帖"),
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(config=config, lark=lark, workflow=workflow, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(
            [(event.message_id, event.action, event.reason) for event in result.events],
            [
                ("om_root", "processed", "created"),
                ("om_orphan_reply", "skipped", "orphan_reply"),
            ],
        )
        self.assertEqual(len(github.created), 1)

    def test_skip_orphan_replies_still_appends_to_existing_issue(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = dataclasses.replace(
            config,
            intake=dataclasses.replace(config.intake, skip_orphan_replies=True),
        )
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                message(message_id="om_reply", root_id="om_root", text="补充信息"),
                message(message_id="om_root", root_id="om_root", text="首次上报"),
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(config=config, lark=lark, workflow=workflow, limit=10)

        # The reply is not orphaned: its root is in the batch, so both coalesce
        # into one create instead of being skipped.
        self.assertEqual(result.processed, 1)
        self.assertEqual(len(result.outcomes), 1)
        self.assertEqual(result.outcomes[0].action, "created")
        self.assertIn("首次上报", github.created[0].issue.body)
        self.assertIn("补充信息", github.created[0].issue.body)

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


class TopicBatchTest(unittest.TestCase):
    def test_scan_topic_batches_groups_by_root_oldest_first(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        lark = FakeLarkHistory(
            [
                # list API order is newest first; scan reverses it.
                message(message_id="om_a2", root_id="om_a1", text="A followup"),
                message(message_id="om_b1", root_id="om_b1", text="B report"),
                message(message_id="om_a1", root_id="om_a1", text="A report"),
                message(message_id="om_bot", root_id="om_bot", sender_open_id=config.lark.bot_open_id),
            ]
        )

        scan = scan_topic_batches(config=config, lark=lark)

        self.assertEqual(scan.scanned, 4)
        self.assertEqual([e.reason for e in scan.skipped_events], ["bot_message"])
        batches = {batch.root_key: [m.message_id for m in batch.messages] for batch in scan.topics}
        self.assertEqual(batches, {"om_a1": ["om_a1", "om_a2"], "om_b1": ["om_b1"]})

    def test_scan_topic_batches_excludes_in_flight_roots_and_ledger(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        lark = FakeLarkHistory(
            [
                message(message_id="om_a1", root_id="om_a1", text="A report"),
                message(message_id="om_b1", root_id="om_b1", text="B report"),
                message(message_id="om_c1", root_id="om_c1", text="C report"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonMessageLedger.load(Path(tmp) / "ledger.json")
            ledger.mark_processed("om_c1")

            scan = scan_topic_batches(
                config=config,
                lark=lark,
                processed_ledger=ledger,
                exclude_roots=frozenset({"om_a1"}),
            )

        self.assertEqual([batch.root_key for batch in scan.topics], ["om_b1"])
        self.assertEqual([e.reason for e in scan.skipped_events], ["processed_ledger"])

    def test_process_topic_batch_write_failure_is_atomic(self) -> None:
        # A coalesced batch is one write; a failure marks nothing processed so
        # the whole topic retries next scan (no half-applied comment spam).
        config = load_project_config(Path("projects/todo-sandbox.toml"))

        class ExplodingWorkflow:
            def has_issue_for_root(self, *, chat_id: str, root_id: str) -> bool:
                return False

            def process_batch(self, records):  # type: ignore[no-untyped-def]
                raise RuntimeError("boom")

        batch = TopicBatch(
            root_key="om_a1",
            messages=(
                message(message_id="om_a1", root_id="om_a1", text="A report"),
                message(message_id="om_a2", root_id="om_a1", text="A followup"),
            ),
        )

        result = process_topic_batch(
            batch,
            config=config,
            lark=FakeLarkHistory([]),
            workflow=ExplodingWorkflow(),  # type: ignore[arg-type]
        )

        self.assertEqual(result.processed_message_ids, ())
        self.assertEqual(result.outcomes, ())
        self.assertIn("boom", result.error)
        self.assertEqual([e.action for e in result.events], ["error"])

    def test_process_topic_batch_orphan_probe_gh_failure_does_not_raise(self) -> None:
        # A transient gh failure (api.github.com EOF) inside the orphan-check's
        # has_issue_for_root probe must become a topic error, not crash the whole
        # watcher; nothing is marked processed so the topic retries next scan.
        from bugpatrol.github import GitHubCliError

        config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = dataclasses.replace(
            config, intake=dataclasses.replace(config.intake, skip_orphan_replies=True)
        )

        class ExplodingOrphanProbe:
            def has_issue_for_root(self, *, chat_id: str, root_id: str) -> bool:
                raise GitHubCliError("gh issue list ...: EOF")

        batch = TopicBatch(
            root_key="om_a1",
            messages=(message(message_id="om_b1", root_id="om_a1", text="An orphan reply"),),
        )

        result = process_topic_batch(
            batch,
            config=config,
            lark=FakeLarkHistory([]),
            workflow=ExplodingOrphanProbe(),  # type: ignore[arg-type]
        )

        self.assertEqual(result.processed_message_ids, ())
        self.assertEqual(result.outcomes, ())
        self.assertIn("GitHubCliError", result.error)
        self.assertEqual([e.action for e in result.events], ["error"])

    def test_process_topic_batch_build_failure_keeps_prefix(self) -> None:
        # A per-message build failure (e.g. attachment materialization) writes
        # the successfully-built prefix and leaves the rest to retry.
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        branch_chat = next(iter(config.lark.branch_chats))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory([])
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        calls = {"n": 0}

        def resolver(_branch: str) -> str:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("boom")
            return "sha1"

        batch = TopicBatch(
            root_key="om_a1",
            messages=(
                message(message_id="om_a1", root_id="om_a1", chat_id=branch_chat, text="A report"),
                message(message_id="om_a2", root_id="om_a1", chat_id=branch_chat, text="A followup"),
            ),
        )

        result = process_topic_batch(
            batch,
            config=config,
            lark=lark,
            workflow=workflow,
            branch_tip_resolver=resolver,
        )

        self.assertEqual(result.processed_message_ids, ("om_a1",))
        self.assertEqual(len(result.outcomes), 1)
        self.assertEqual(result.outcomes[0].action, "created")
        self.assertIn("boom", result.error)
        self.assertEqual([e.action for e in result.events], ["error", "processed"])

    def test_process_topic_batch_intercepts_slash_command(self) -> None:
        # A `/fix` reply in a topic is consumed by the slash handler: it is
        # marked processed and never re-enters intake (no issue created).
        from bugpatrol.slash_commands import SlashCommandHandler

        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        # Register an issue mapped to this topic root so /fix resolves it.
        github.create_issue(
            repo=config.github_repo,
            title="bug",
            body=f'<!-- {{"chat_id":"{config.lark.chat_id}","root_id":"om_a1"}} -->',
            issue_type="Bug",
            fields={},
        )
        calls: list[int] = []
        handler = SlashCommandHandler(
            config=config, github=github, lark=lark, fix_dispatch=calls.append
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        batch = TopicBatch(
            root_key="om_a1",
            messages=(
                message(
                    message_id="om_a1",
                    root_id="om_a1",
                    chat_id=config.lark.chat_id,
                    text="/fix",
                ),
            ),
        )

        result = process_topic_batch(
            batch,
            config=config,
            lark=lark,
            workflow=workflow,
            slash_handler=handler,
        )

        self.assertEqual(result.processed_message_ids, ("om_a1",))
        self.assertEqual(result.outcomes, ())
        self.assertEqual([e.reason for e in result.events], ["slash_fix"])
        self.assertEqual(calls, [1])
        # Only the pre-registered issue exists; /fix did not create a new one.
        self.assertEqual(len(github.created), 1)

    # --- merged forward (转发聊天记录) ---

    def _forward_items(self) -> list[dict[str, object]]:
        return [
            raw_lark_item(
                message_id="om_a",
                msg_type="text",
                content=json.dumps({"text": "post有标题只展示两行正文，这个算bug么"}, ensure_ascii=False),
                sender_id="ou_azer",
            ),
            raw_lark_item(
                message_id="om_b",
                msg_type="image",
                content=json.dumps({"image_key": "img_v2_x"}),
                sender_id="ou_azer",
            ),
            raw_lark_item(
                message_id="om_c",
                msg_type="text",
                content=json.dumps({"text": "不会显示全部正文，最多显示4行"}, ensure_ascii=False),
                sender_id="ou_irisy",
            ),
        ]

    def test_expand_merge_forward_replays_transcript(self) -> None:
        backfill_module._chat_members_cache.clear()
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        envelope = merged_forward_message()
        lark = FakeLarkMergedForward(
            [envelope],
            self._forward_items(),
            member_names={"ou_azer": "Azer", "ou_irisy": "Irisy"},
        )

        expanded = expand_merge_forward(lark, envelope)

        self.assertIsNotNone(expanded)
        self.assertEqual(expanded.message_id, "om_env")
        self.assertEqual(expanded.root_id, "om_env")
        self.assertEqual(expanded.msg_type, "text")
        self.assertIn("Azer：post有标题只展示两行正文，这个算bug么", expanded.text)
        self.assertIn("Irisy：不会显示全部正文，最多显示4行", expanded.text)
        self.assertIn("[图片/附件", expanded.text)
        self.assertEqual(lark.forward_fetches, ["om_env"])

    def test_expand_merge_forward_returns_none_when_unexpandable(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        envelope = merged_forward_message()
        # Detail returns only the envelope itself -> nothing reportable inside.
        lark = FakeLarkMergedForward(
            [envelope],
            [raw_lark_item(message_id="om_env", msg_type="merge_forward", content="Merged and Forwarded Message")],
        )

        self.assertIsNone(expand_merge_forward(lark, envelope))
        # A client that cannot expand (raises) is also a None, retried next scan.
        self.assertIsNone(expand_merge_forward(FakeLarkHistory([envelope]), envelope))

    def test_scan_topic_batches_expands_merged_forward_with_its_followups(self) -> None:
        backfill_module._chat_members_cache.clear()
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        chat_id = config.lark.chat_id
        followup = message(
            message_id="om_follow",
            root_id="om_env",
            chat_id=chat_id,
            sender_open_id="ou_irisy",
            create_time="1788322391000",
            msg_type="text",
            text="@Lucy 这个我们补一个UI样式",
        )
        lark = FakeLarkMergedForward(
            [followup, merged_forward_message(chat_id=chat_id, create_time="1788322389929")],
            self._forward_items(),
            member_names={"ou_azer": "Azer", "ou_irisy": "Irisy"},
        )

        result = scan_topic_batches(config=config, lark=lark, limit=10, chat_id=chat_id)

        self.assertEqual(result.skipped_events, ())
        self.assertEqual(len(result.topics), 1)
        batch = result.topics[0]
        self.assertEqual(batch.root_key, "om_env")
        self.assertEqual([m.message_id for m in batch.messages], ["om_env", "om_follow"])
        self.assertEqual(batch.messages[0].msg_type, "text")
        self.assertIn("Azer：post有标题只展示两行正文，这个算bug么", batch.messages[0].text)
        self.assertEqual(batch.messages[1].text, "@Lucy 这个我们补一个UI样式")
        self.assertEqual(lark.forward_fetches, ["om_env"])

    def test_scan_topic_batches_skips_processed_merged_forward_without_re_fetch(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        envelope = merged_forward_message()
        lark = FakeLarkMergedForward([envelope], self._forward_items())

        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonMessageLedger.load(Path(tmp) / "processed.json")
            ledger.mark_processed("om_env")
            result = scan_topic_batches(
                config=config,
                lark=lark,
                limit=10,
                chat_id=config.lark.chat_id,
                processed_ledger=ledger,
            )

        self.assertEqual(lark.forward_fetches, [])
        self.assertEqual(result.topics, ())
        self.assertIn("processed_ledger", [event.reason for event in result.skipped_events])

    def test_scan_topic_batches_skips_merged_forward_with_no_content(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        envelope = merged_forward_message()
        lark = FakeLarkMergedForward([envelope], [])

        result = scan_topic_batches(config=config, lark=lark, limit=10, chat_id=config.lark.chat_id)

        self.assertEqual(result.topics, ())
        self.assertIn("merge_forward_unexpandable", [event.reason for event in result.skipped_events])

    def test_run_lark_backfill_creates_issue_from_merged_forward(self) -> None:
        backfill_module._chat_members_cache.clear()
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        chat_id = config.lark.chat_id
        followup = message(
            message_id="om_follow",
            root_id="om_env",
            chat_id=chat_id,
            sender_open_id="ou_irisy",
            msg_type="text",
            text="@Lucy 这个我们补一个UI样式",
        )
        lark = FakeLarkMergedForward(
            [followup, merged_forward_message(chat_id=chat_id)],
            self._forward_items(),
            member_names={"ou_azer": "Azer", "ou_irisy": "Irisy"},
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(config=config, lark=lark, workflow=workflow, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.outcomes[0].action, "created")
        self.assertEqual(len(github.created), 1)
        body = github.created[0].issue.body
        self.assertIn("Azer：post有标题只展示两行正文，这个算bug么", body)
        self.assertIn("@Lucy 这个我们补一个UI样式", body)
        self.assertEqual(
            set(result.outcomes[0].triage_signal.material_message_ids),
            {"om_env", "om_follow"},
        )


if __name__ == "__main__":
    unittest.main()
