from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient
from bugpatrol.triage_result import apply_triage_result, parse_triage_result


class FakeIssueFields:
    def __init__(self) -> None:
        self.writes: list[dict[str, object]] = []

    def get_issue_field_values(self, **kwargs: object) -> dict[str, str]:
        return {}

    def add_issue_field_values(self, **kwargs: object) -> None:
        self.writes.append(kwargs)


class NeedsInfoLoopE2ETest(unittest.TestCase):
    def test_intake_then_needs_info_triage_sends_lark_follow_up_once(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        intake = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_reporter",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_root",
                original_text="Todo 删除最后一项后页面空白",
            )
        )
        result = parse_triage_result(
            {
                "issue_type": "Bug",
                "priority": "Medium",
                "triage_status": "Needs info",
                "triage_verdict": "信息不足",
                "platform": "Web",
                "reproducibility": "未知",
                "other_platforms": "未验证",
                "capability": "Quest",
                "evidence": "文字描述",
                "prd_status": "未校验",
                "triage_confidence": "低",
                "assignee": "garlanddiego",
                "owner_reason": "Manual",
                "follow_up_questions": ["请补充浏览器版本", "请确认刷新后是否恢复"],
                "comment_markdown": "## Triage Analysis\n\n需要补充信息。",
            }
        )
        issue_fields = FakeIssueFields()

        first = apply_triage_result(
            repo=config.github_repo,
            issue_number=intake.issue.number,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
            lark=lark,
        )
        second = apply_triage_result(
            repo=config.github_repo,
            issue_number=intake.issue.number,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertTrue(first.comment_added)
        self.assertFalse(second.comment_added)
        self.assertEqual(len(github.created), 1)
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertIn("BUGPATROL_TRIAGE_META", github.created[0].comments[0])
        self.assertEqual(len(lark.replies), 3)
        self.assertIn("已创建 GitHub issue", lark.replies[0].text)
        self.assertIn("请补充浏览器版本", lark.replies[1].text)
        self.assertEqual(lark.replies[1].message_id, "om_root")
        # The idempotent re-run doesn't repeat the follow-up; it pings the topic
        # once so the reporter knows the run completed and didn't hang.
        self.assertIn("结论无变化", lark.replies[2].text)
        self.assertEqual(lark.replies[2].message_id, "om_root")


if __name__ == "__main__":
    unittest.main()
