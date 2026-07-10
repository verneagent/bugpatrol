from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.reconcile_triage import reconcile_triage
from bugpatrol.testing.fakes import FakeGitHubIssuesClient

INTAKE_META = '<!-- BUGPATROL_INTAKE_META:{"source":"lark","chat_id":"oc_x","root_id":"om_r"} -->'
TRIAGE_META_COMMENT = '<!-- BUGPATROL_TRIAGE_META\n{"verdict":"bug"}\nBUGPATROL_TRIAGE_META -->'


class ReconcileTriageLoopE2ETest(unittest.TestCase):
    def test_outage_replay_is_idempotent_across_two_passes(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        issue = github.create_issue(
            repo=config.github_repo,
            title="untriaged after outage",
            body=f"报告\n{INTAKE_META}",
            issue_type="Bug",
            fields={},
        )

        # First replay picks up the managed issue that never got a triage result
        # and "runs" triage (which writes the triage-meta comment).
        def run_triage(issue_number: int) -> None:
            github.add_issue_comment(
                repo=config.github_repo,
                issue_number=issue_number,
                body=TRIAGE_META_COMMENT,
            )

        first = reconcile_triage(
            config=config, github=github, execute=True, run_triage=run_triage
        )
        self.assertIn(
            (issue.number, "triaged", "executed"),
            [(e.issue_number, e.action, e.reason) for e in first.events],
        )

        # Second replay must skip it — the triage result now exists, so a repeated
        # reconcile after an outage does not re-triage.
        ran_again: list[int] = []
        second = reconcile_triage(
            config=config, github=github, execute=True, run_triage=ran_again.append
        )
        self.assertEqual(ran_again, [])
        self.assertEqual(second.candidates, ())
        self.assertIn(
            (issue.number, "skipped", "already_triaged"),
            [(e.issue_number, e.action, e.reason) for e in second.events],
        )


if __name__ == "__main__":
    unittest.main()
