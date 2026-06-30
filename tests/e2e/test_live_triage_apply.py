from __future__ import annotations

import os
import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.triage_result import apply_triage_result, parse_triage_result


@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_E2E") == "1", "live e2e is opt-in")
class LiveTriageApplyE2ETest(unittest.TestCase):
    def test_live_apply_triage_result_writes_type_fields_comment_and_assignee(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = GitHubCliIssuesClient()
        issue_fields = GitHubIssueFieldsClient()
        issue = github.create_issue(
            repo=config.github_repo,
            title="[test] live triage apply e2e",
            body="temporary issue for live triage apply e2e",
            issue_type="Bug",
            fields={},
        )
        try:
            result = parse_triage_result(
                {
                    "issue_type": "Bug",
                    "priority": "High",
                    "triage_status": "Done",
                    "triage_verdict": "代码 Bug",
                    "platform": "Web",
                    "reproducibility": "必现",
                    "other_platforms": "未验证",
                    "capability": "Quest",
                    "evidence": "文字描述",
                    "prd_status": "已对齐",
                    "triage_confidence": "高",
                    "assignee": "garlanddiego",
                    "owner_reason": "CODEOWNERS",
                    "comment_markdown": "## Triage Analysis\n\nLive e2e triage apply.",
                }
            )
            apply_triage_result(
                repo=config.github_repo,
                issue_number=issue.number,
                config=config,
                result=result,
                github=github,
                issue_fields=issue_fields,
            )
            self.assertEqual(github.get_issue_type(repo=config.github_repo, issue_number=issue.number), "Bug")
            values = issue_fields.get_issue_field_values(repo=config.github_repo, issue_number=issue.number)
            self.assertEqual(values["Triage verdict"], "代码 Bug")
            self.assertEqual(values["Triage status"], "Done")
            self.assertEqual(values["Priority"], "High")
            updated = github.get_issue(repo=config.github_repo, issue_number=issue.number)
            self.assertIn("live triage apply e2e", updated.title)
        finally:
            github.close_issue(repo=config.github_repo, issue_number=issue.number)


if __name__ == "__main__":
    unittest.main()

