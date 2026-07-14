from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.clients import GitHubIssue
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
        self.assertEqual(github.created[0].repo, "TheCloverLab/bugpatrol-todo-sandbox")
        self.assertEqual(github.created[0].issue_type, "Bug")
        self.assertEqual(github.created[0].fields["Triage status"], "Pending")
        self.assertIn("BUGPATROL_INTAKE_META", github.created[0].issue.body)
        self.assertIn("## Lark 上报", github.created[0].issue.body)
        self.assertIn("## 原始消息", github.created[0].issue.body)
        self.assertEqual(len(lark.replies), 1)
        self.assertIn("已创建 GitHub issue [#1](", lark.replies[0].text)

    def test_process_survives_withdrawn_reply_target(self) -> None:
        from bugpatrol.lark import LarkOpenApiError

        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        error = LarkOpenApiError(
            'Lark HTTP 400: {"code":230011,"msg":"The message was withdrawn."}'
        )

        def failing_reply(**kwargs: object) -> None:
            raise error

        lark.reply_to_message = failing_reply  # type: ignore[method-assign]
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        outcome = workflow.process(make_record())

        self.assertEqual(outcome.action, "created")
        self.assertEqual(len(github.created), 1)
        self.assertIn("原消息已撤回", outcome.lark_reply)

    def test_process_reraises_non_withdrawn_reply_errors(self) -> None:
        from bugpatrol.lark import LarkOpenApiError

        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()

        def failing_reply(**kwargs: object) -> None:
            raise LarkOpenApiError('Lark HTTP 500: {"code":9999,"msg":"boom"}')

        lark.reply_to_message = failing_reply  # type: ignore[method-assign]
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        with self.assertRaises(LarkOpenApiError):
            workflow.process(make_record())

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
        self.assertIn("已追加到 GitHub issue [#1](", lark.replies[1].text)

    def test_material_followup_after_done_marks_pending(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        issue_fields = FakeIssueFields({"Triage status": "Done"})
        workflow = IntakeWorkflow(config=config, github=github, lark=lark, issue_fields=issue_fields)

        workflow.process(make_record(message_id="om_first", original_text="首次上报"))
        outcome = workflow.process(make_record(message_id="om_second", original_text="补充：安卓也会卡住"))

        self.assertEqual(outcome.action, "updated")
        self.assertEqual(issue_fields.writes[-1]["values"], {"Triage status": "Pending"})

    def test_ack_followup_after_done_does_not_change_status(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        issue_fields = FakeIssueFields({"Triage status": "Done"})
        workflow = IntakeWorkflow(config=config, github=github, lark=lark, issue_fields=issue_fields)

        workflow.process(make_record(message_id="om_first", original_text="首次上报"))
        workflow.process(make_record(message_id="om_second", original_text="收到"))

        self.assertEqual(issue_fields.writes, [])

    def test_followup_heals_missing_intake_fields_and_forces_enqueue(self) -> None:
        # Crash window: issue created but watcher died before writing fields.
        # The replayed message dedupes into the followup path, which must
        # backfill the initial fields and enqueue triage even for an ack.
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        issue_fields = FakeIssueFields({})
        workflow = IntakeWorkflow(config=config, github=github, lark=lark, issue_fields=issue_fields)

        workflow.process(make_record(message_id="om_first", original_text="首次上报"))
        outcome = workflow.process(make_record(message_id="om_second", original_text="收到"))

        self.assertEqual(outcome.action, "updated")
        self.assertEqual(len(issue_fields.writes), 1)
        values = issue_fields.writes[0]["values"]
        self.assertEqual(values["Triage status"], "Pending")
        self.assertEqual(values["Source"], "Lark")
        self.assertTrue(outcome.triage_signal.should_enqueue)
        self.assertEqual(outcome.triage_signal.reason, "healed_missing_fields")

    def test_followup_does_not_heal_when_status_present(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        issue_fields = FakeIssueFields({"Triage status": "Needs info"})
        workflow = IntakeWorkflow(config=config, github=github, lark=lark, issue_fields=issue_fields)

        workflow.process(make_record(message_id="om_first", original_text="首次上报"))
        workflow.process(make_record(message_id="om_second", original_text="收到"))

        self.assertEqual(issue_fields.writes, [])

    def test_process_skips_already_recorded_message(self) -> None:
        # A watcher replay or a backfill re-scan feeds the same message again;
        # it must not append a second follow-up comment or re-notify Lark.
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        workflow.process(make_record(message_id="om_first", original_text="首次上报"))
        workflow.process(make_record(message_id="om_second", original_text="补充"))
        outcome = workflow.process(make_record(message_id="om_second", original_text="补充"))

        self.assertEqual(outcome.action, "duplicate")
        self.assertFalse(outcome.triage_signal.should_enqueue)
        # Still exactly one follow-up comment and two Lark replies (create + first
        # follow-up); the duplicate produced neither.
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertEqual(len(lark.replies), 2)

    def test_process_batch_appends_only_new_messages(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        workflow.process(make_record(message_id="om_a", original_text="首次"))
        outcome = workflow.process_batch(
            [
                make_record(message_id="om_a", original_text="首次"),
                make_record(message_id="om_b", original_text="补充1"),
                make_record(message_id="om_c", original_text="补充2"),
            ]
        )

        self.assertEqual(outcome.action, "updated")
        self.assertEqual(len(github.created[0].comments), 1)
        # Only the two new messages are in the follow-up; the recorded one is not.
        self.assertIn("om_b", github.created[0].comments[0])
        self.assertIn("om_c", github.created[0].comments[0])
        self.assertNotIn("om_a", github.created[0].comments[0])
        self.assertIn("已追加 2 条", lark.replies[-1].text)

    def test_process_batch_all_recorded_is_a_duplicate(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        workflow.process(make_record(message_id="om_a", original_text="首次"))
        workflow.process(make_record(message_id="om_b", original_text="补充"))
        replies_before = len(lark.replies)
        outcome = workflow.process_batch(
            [
                make_record(message_id="om_a", original_text="首次"),
                make_record(message_id="om_b", original_text="补充"),
            ]
        )

        self.assertEqual(outcome.action, "duplicate")
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertEqual(len(lark.replies), replies_before)

    def test_process_deduplicates_create_race_without_commenting(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = RaceGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        outcome = workflow.process(make_record())

        self.assertEqual(outcome.action, "deduplicated")
        self.assertEqual(outcome.issue.number, 9)
        self.assertFalse(outcome.triage_signal.should_enqueue)
        self.assertEqual(github.created, [])
        self.assertEqual(github.comments, [])
        self.assertIn("已创建 GitHub issue [#9](", lark.replies[0].text)

    def test_rejects_records_from_unconfigured_chat(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        workflow = IntakeWorkflow(
            config=config,
            github=FakeGitHubIssuesClient(),
            lark=FakeLarkMessengerClient(),
        )

        with self.assertRaisesRegex(ValueError, "unexpected chat_id"):
            workflow.process(make_record(chat_id="oc_other"))


class RaceGitHubIssuesClient(FakeGitHubIssuesClient):
    def __init__(self) -> None:
        super().__init__()
        self.find_calls = 0
        self.comments: list[str] = []

    def find_issue_by_intake_root(self, *, repo: str, chat_id: str, root_id: str) -> GitHubIssue | None:
        self.find_calls += 1
        if self.find_calls == 1:
            return None
        return GitHubIssue(
            number=9,
            url="https://github.test/o/r/issues/9",
            title="[Lark] existing",
            body="existing body",
        )

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        self.comments.append(body)


class FakeIssueFields:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.writes: list[dict[str, object]] = []

    def get_issue_field_values(self, *, repo: str, issue_number: int) -> dict[str, str]:
        return self.values

    def add_issue_field_values(self, **kwargs: object) -> None:
        self.writes.append(kwargs)


if __name__ == "__main__":
    unittest.main()
