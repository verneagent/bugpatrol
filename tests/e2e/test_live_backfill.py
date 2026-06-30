from __future__ import annotations

import os
import unittest
from pathlib import Path

from bugpatrol.backfill import run_lark_backfill
from bugpatrol.config import load_project_config
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkOpenApiMessengerClient


@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_E2E") == "1", "live e2e is opt-in")
class LiveBackfillE2ETest(unittest.TestCase):
    def test_live_lark_backfill_dry_run_reads_history_without_writes(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        app_secret = os.environ[config.lark.app_secret_env]
        lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        github = GitHubCliIssuesClient(
            issue_fields=GitHubIssueFieldsClient(),
            project_config=config,
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(
            config=config,
            lark=lark,
            workflow=workflow,
            limit=8,
            dry_run=True,
        )

        self.assertGreaterEqual(result.scanned, 1)
        self.assertEqual(result.processed, 0)
        self.assertEqual(result.scanned, result.skipped)
        self.assertEqual(result.outcomes, ())


if __name__ == "__main__":
    unittest.main()

