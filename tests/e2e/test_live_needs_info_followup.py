from __future__ import annotations

import os
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkOpenApiMessengerClient
from bugpatrol.triage_result import apply_triage_result, parse_triage_result


@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_E2E") == "1", "live e2e is opt-in")
class LiveNeedsInfoFollowUpE2ETest(unittest.TestCase):
    def test_live_needs_info_follow_up_is_sent_once(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        app_secret = os.environ[config.lark.app_secret_env]
        issue_fields = GitHubIssueFieldsClient()
        github = GitHubCliIssuesClient(
            issue_fields=issue_fields,
            project_config=config,
        )
        lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        unique_question = f"[test] 请补充浏览器版本 {stamp}"
        created_issue_number: int | None = None

        try:
            seed = lark.send_chat_message(
                chat_id=config.lark.chat_id,
                text=f"BugPatrol live needs-info seed {stamp}.",
            )
            intake = workflow.process(
                IntakeRecord(
                    reporter_name="BugPatrol Live E2E",
                    reporter_open_id=config.lark.bot_open_id,
                    created_at=datetime.now(UTC).isoformat(),
                    chat_id=config.lark.chat_id,
                    root_id=f"live_needs_info_{stamp}",
                    message_id=seed.message_id,
                    original_text=f"[test] Todo needs info {stamp}",
                )
            )
            created_issue_number = intake.issue.number
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
                    "follow_up_questions": [unique_question],
                    "comment_markdown": "## Triage Analysis\n\nNeeds live follow-up info.",
                }
            )

            first = apply_triage_result(
                repo=config.github_repo,
                issue_number=intake.issue.number,
                config=config,
                result=result,
                github=github,
                issue_fields=issue_fields,
                lark=lark,
            )
            second = apply_triage_result(
                repo=config.github_repo,
                issue_number=intake.issue.number,
                config=config,
                result=result,
                github=github,
                issue_fields=issue_fields,
                lark=lark,
            )

            self.assertTrue(first.comment_added)
            self.assertFalse(second.comment_added)
            self.assertEqual(
                _recent_message_count(lark=lark, chat_id=config.lark.chat_id, needle=unique_question),
                1,
            )
        finally:
            if created_issue_number is not None:
                github.close_issue(repo=config.github_repo, issue_number=created_issue_number)


def _recent_message_count(*, lark: LarkOpenApiMessengerClient, chat_id: str, needle: str) -> int:
    for _ in range(5):
        messages = lark.list_chat_messages(chat_id=chat_id, limit=20)
        count = sum(1 for message in messages if needle in message.text)
        if count:
            return count
        time.sleep(1)
    return 0


if __name__ == "__main__":
    unittest.main()
