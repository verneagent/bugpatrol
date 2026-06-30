from __future__ import annotations

import unittest
import json
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.intake import intake_record_from_dict
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient


class IntakeLoopE2ETest(unittest.TestCase):
    def test_lark_topic_to_github_issue_then_followup_comment(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        records = [
            intake_record_from_dict(item)
            for item in json.loads(Path("tests/fixtures/intake_topic_loop.json").read_text())
        ]

        first = workflow.process(records[0])
        second = workflow.process(records[1])

        self.assertEqual(first.action, "created")
        self.assertEqual(second.action, "updated")
        self.assertEqual(len(github.created), 1)
        created = github.created[0]
        self.assertEqual(created.issue_type, "Bug")
        self.assertEqual(created.fields["Source"], "Lark")
        self.assertEqual(created.fields["Intake version"], "v2")
        self.assertEqual(created.fields["Triage status"], "Pending")
        self.assertEqual(created.fields["Evidence"], "截图")
        self.assertIn("Todo 列表删除最后一项后", created.issue.body)
        self.assertIn("## Lark 上报", created.issue.body)
        self.assertEqual(len(created.comments), 1)
        self.assertIn("Web 必现", created.comments[0])
        self.assertIn("## Lark 话题更新", created.comments[0])
        self.assertEqual([reply.message_id for reply in lark.replies], ["om_topic_1", "om_topic_2"])
        self.assertIn(created.issue.url, lark.replies[0].text)
        self.assertIn(created.issue.url, lark.replies[1].text)


if __name__ == "__main__":
    unittest.main()
