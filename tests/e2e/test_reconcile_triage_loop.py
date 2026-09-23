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
        # and dispatches the triage workflow for it. Reconcile fire-and-forgets
        # (a fresh App token per run) rather than running the agent in-process,
        # so the dispatch — not a returned verdict — is what it reports.
        first = reconcile_triage(config=config, github=github, execute=True)
        self.assertEqual(github.dispatched, [(config.github_repo, issue.number)])
        self.assertIn(
            (issue.number, "dispatched", "triage_workflow"),
            [(e.issue_number, e.action, e.reason) for e in first.events],
        )

        # The dispatched workflow finishes and writes its triage result.
        github.add_issue_comment(
            repo=config.github_repo,
            issue_number=issue.number,
            body=TRIAGE_META_COMMENT,
        )

        # Second replay must skip it — the triage result now exists, so a repeated
        # reconcile after an outage does not re-triage.
        second = reconcile_triage(config=config, github=github, execute=True)
        self.assertEqual(len(github.dispatched), 1)
        self.assertEqual(second.candidates, ())
        self.assertIn(
            (issue.number, "skipped", "already_triaged"),
            [(e.issue_number, e.action, e.reason) for e in second.events],
        )


if __name__ == "__main__":
    unittest.main()
