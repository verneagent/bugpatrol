from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.intake_workflow import (
    IntakeWorkflow,
    build_issue_title,
    infer_evidence,
    initial_intake_fields,
)
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient


def make_record(**overrides: object) -> IntakeRecord:
    values = {
        "reporter_name": "Diego",
        "reporter_open_id": "ou_reporter",
        "created_at": "2026-06-30T10:00:00Z",
        "chat_id": "oc_d371f022f168b567a141ced142691894",
        "root_id": "om_root",
        "message_id": "om_msg",
        "original_text": "发完图片后卡在 thinking",
        "lark_topic_url": "https://lark.example/topic/om_root",
        "attachments": (),
    }
    values.update(overrides)
    return IntakeRecord(**values)  # type: ignore[arg-type]


class IntakeWorkflowTest(unittest.TestCase):
    def test_build_issue_title_is_deterministic_and_bounded(self) -> None:
        title = build_issue_title(make_record(original_text="第一行\n\n第二行" * 30))

        self.assertTrue(title.startswith("[Lark] 第一行"))
        self.assertLessEqual(len(title), 87)

    def test_initial_fields_are_facts_only(self) -> None:
        fields = initial_intake_fields(
            make_record(attachments=(Attachment(kind="screenshot", url="https://assets/s.png"),))
        )

        self.assertEqual(
            fields,
            {
                "Source": "Lark",
                "Intake version": "v2",
                "Triage status": "Pending",
                "Evidence": "截图",
            },
        )

    def test_infer_evidence_handles_multiple_media_types(self) -> None:
        evidence = infer_evidence(
            (
                Attachment(kind="screenshot", url="https://assets/s.png"),
                Attachment(kind="video", url="https://assets/v.mp4"),
            ),
            "",
        )

        self.assertEqual(evidence, "多种")

    def test_process_creates_issue_and_replies_to_lark(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        outcome = workflow.process(make_record())

        self.assertEqual(outcome.action, "created")
        self.assertEqual(outcome.issue.number, 1)
        self.assertEqual(github.created[0].repo, "verneagent/bugpatrol-todo-sandbox")
        self.assertEqual(github.created[0].issue_type, "Bug")
        self.assertEqual(github.created[0].fields["Triage status"], "Pending")
        self.assertIn("BUGPATROL_INTAKE_META", github.created[0].issue.body)
        self.assertIn("## Lark 上报", github.created[0].issue.body)
        self.assertIn("## 原始消息", github.created[0].issue.body)
        self.assertEqual(len(lark.replies), 1)
        self.assertIn("已创建 GitHub issue #1", lark.replies[0].text)

    def test_process_appends_followup_for_same_topic_root(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        workflow.process(make_record(message_id="om_first", original_text="首次上报"))
        outcome = workflow.process(make_record(message_id="om_second", original_text="补充：安卓也会卡住"))

        self.assertEqual(outcome.action, "updated")
        self.assertEqual(len(github.created), 1)
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertIn("补充：安卓也会卡住", github.created[0].comments[0])
        self.assertIn("BUGPATROL_INTAKE_REPLY_META", github.created[0].comments[0])
        self.assertIn("## Lark 话题更新", github.created[0].comments[0])
        self.assertIn("## 消息", github.created[0].comments[0])
        self.assertEqual(len(lark.replies), 2)
        self.assertIn("已追加到 GitHub issue #1", lark.replies[1].text)

    def test_rejects_records_from_unconfigured_chat(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        workflow = IntakeWorkflow(
            config=config,
            github=FakeGitHubIssuesClient(),
            lark=FakeLarkMessengerClient(),
        )

        with self.assertRaisesRegex(ValueError, "unexpected chat_id"):
            workflow.process(make_record(chat_id="oc_other"))


if __name__ == "__main__":
    unittest.main()
