from __future__ import annotations

import unittest

from bugpatrol.intake import (
    Attachment,
    IntakeRecord,
    branch_tip_sha_from_metadata,
    format_created_at,
    intake_record_from_dict,
    parse_intake_metadata,
    render_issue_body,
    target_branch_from_metadata,
)


class IntakeTest(unittest.TestCase):
    def test_render_issue_body_records_facts_without_triage(self) -> None:
        body = render_issue_body(
            IntakeRecord(
                reporter_name="Diego",
                reporter_open_id="ou_123",
                created_at="2026-06-30T10:00:00Z",
                chat_id="oc_123",
                root_id="om_root",
                message_id="om_msg",
                original_text="发完图片后卡在 thinking",
                lark_topic_url="https://example.test/topic",
                attachments=(
                    Attachment(
                        kind="screenshot",
                        url="https://assets.example/s1.png",
                        description="generated: buddy chat screen",
                    ),
                ),
            )
        )

        self.assertIn("## Lark Intake", body)
        self.assertIn("发完图片后卡在 thinking", body)
        self.assertIn("BUGPATROL_INTAKE_META", body)
        self.assertIn('"schema_version":1', body)
        self.assertIn('"source":"lark"', body)
        self.assertNotIn("Triage verdict", body)
        self.assertNotIn("代码 Bug", body)

    def test_render_issue_body_can_use_chinese_copy(self) -> None:
        body = render_issue_body(
            IntakeRecord(
                reporter_name="QA",
                reporter_open_id="ou_qa",
                created_at="1783099728900",
                chat_id="oc_123",
                root_id="om_root",
                message_id="om_msg",
                original_text="删除最后一项后空状态没出现",
                lark_topic_url="https://applink.larksuite.com/client/chat/open?openChatId=oc_123&messageId=om_root",
                lark_message_url="https://applink.larksuite.com/client/chat/open?openChatId=oc_123&messageId=om_msg",
                attachments=(
                    Attachment(
                        kind="image",
                        url="https://assets.example/s1.png",
                        description="todo 空列表页面",
                    ),
                ),
            ),
            language="zh-CN",
        )

        self.assertIn("## Lark 上报", body)
        self.assertIn("- 上报人: QA (ou_qa)", body)
        self.assertIn("- 创建时间: 2026-07-03 17:28:48 UTC (1783099728900)", body)
        self.assertIn("- Lark 话题: [打开话题](https://applink.larksuite.com/client/chat/open?openChatId=oc_123&messageId=om_root) (`om_root`)", body)
        self.assertIn("- 消息 ID: [打开消息](https://applink.larksuite.com/client/chat/open?openChatId=oc_123&messageId=om_msg) (`om_msg`)", body)
        self.assertIn("## 原始消息", body)
        self.assertIn("## 附件", body)
        self.assertIn("![图片 1](https://assets.example/s1.png)", body)
        self.assertIn("生成描述: todo 空列表页面", body)

    def test_format_created_at_keeps_existing_readable_values(self) -> None:
        self.assertEqual(format_created_at("2026-06-30T10:00:00Z"), "2026-06-30T10:00:00Z")

    def test_intake_record_from_dict_parses_attachments(self) -> None:
        record = intake_record_from_dict(
            {
                "reporter_name": "QA",
                "reporter_open_id": "ou_qa",
                "created_at": "2026-06-30T10:00:00Z",
                "chat_id": "oc_123",
                "root_id": "om_root",
                "message_id": "om_msg",
                "original_text": "todo 删除失败",
                "attachments": [{"kind": "screenshot", "url": "https://assets/s.png"}],
            }
        )

        self.assertEqual(record.attachments[0].kind, "screenshot")
        self.assertEqual(record.attachments[0].url, "https://assets/s.png")

    def test_intake_record_from_dict_rejects_missing_required_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "reporter_name"):
            intake_record_from_dict({})

    def test_main_branch_meta_omits_branch_keys(self) -> None:
        body = render_issue_body(
            IntakeRecord(
                reporter_name="QA",
                reporter_open_id="ou_qa",
                created_at="2026-06-30T10:00:00Z",
                chat_id="oc_123",
                root_id="om_root",
                message_id="om_msg",
                original_text="main-branch bug",
            )
        )
        metadata = parse_intake_metadata(body)

        assert metadata is not None
        self.assertNotIn("target_branch", metadata)
        self.assertNotIn("branch_tip_sha", metadata)
        self.assertEqual(target_branch_from_metadata(metadata), "main")
        self.assertEqual(branch_tip_sha_from_metadata(metadata), "")

    def test_feature_branch_meta_stamps_branch_and_tip(self) -> None:
        body = render_issue_body(
            IntakeRecord(
                reporter_name="QA",
                reporter_open_id="ou_qa",
                created_at="2026-06-30T10:00:00Z",
                chat_id="oc_feature",
                root_id="om_root",
                message_id="om_msg",
                original_text="feature-branch bug",
                target_branch="feature/x",
                branch_tip_sha="abc123def456",
            )
        )
        metadata = parse_intake_metadata(body)

        assert metadata is not None
        self.assertEqual(target_branch_from_metadata(metadata), "feature/x")
        self.assertEqual(branch_tip_sha_from_metadata(metadata), "abc123def456")

    def test_feature_branch_without_tip_omits_tip_key(self) -> None:
        body = render_issue_body(
            IntakeRecord(
                reporter_name="QA",
                reporter_open_id="ou_qa",
                created_at="2026-06-30T10:00:00Z",
                chat_id="oc_feature",
                root_id="om_root",
                message_id="om_msg",
                original_text="feature-branch bug",
                target_branch="feature/x",
            )
        )
        metadata = parse_intake_metadata(body)

        assert metadata is not None
        self.assertEqual(target_branch_from_metadata(metadata), "feature/x")
        self.assertNotIn("branch_tip_sha", metadata)


if __name__ == "__main__":
    unittest.main()
