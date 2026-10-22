from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from bugpatrol.agents import AgentInvocation
from bugpatrol.config import load_project_config
from bugpatrol.testing.fakes import FakeGitHubIssueFieldsClient, FakeGitHubIssuesClient
from bugpatrol.triage_ownership import (
    TriageWriteSnapshot,
    TriageWriteSuperseded,
    append_triage_run_metadata,
)
from bugpatrol.triage_result import apply_triage_result, parse_triage_metadata, parse_triage_result
from bugpatrol.triage_runner import (
    TriageRunPlan,
    execute_triage_run,
    prepare_triage_run,
    triage_run_in_flight,
)

RESULT = {
    "issue_type": "Bug", "priority": "High", "triage_status": "Done",
    "triage_verdict": "代码 Bug", "platform": "Web", "reproducibility": "必现",
    "other_platforms": "未验证", "capability": "Quest", "evidence": "日志",
    "prd_status": "已对齐", "triage_confidence": "高", "assignee": "bot-proposed-owner",
    "owner_reason": "CODEOWNERS", "suspected_owner": "suspected-introducer",
    "corrected_title": "Corrected title", "comment_markdown": "Review complete.",
}


class TriageOwnershipLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_project_config(Path("projects/todo-sandbox.toml"))
        self.github = FakeGitHubIssuesClient()
        self.github.create_issue(
            repo=self.config.github_repo, title="Initial title",
            body='<!-- BUGPATROL_INTAKE_META:{"source":"lark","chat_id":"test","root_id":"test"} -->',
            issue_type="Task", fields={"Triage status": "Running", "Priority": "Low", "Owner": "original-introducer", "Owner reason": "Original reason"},
        )
        self.fields = FakeGitHubIssueFieldsClient(self.github)
        self.item = self.github.created[0]
        self.snapshot = TriageWriteSnapshot(self.item.issue, dict(self.item.fields), self.github.get_issue_write_history(repo=self.config.github_repo, issue_number=1))
        self.github.add_issue_comment(repo=self.config.github_repo, issue_number=1, body=append_triage_run_metadata({"run_id": "current"}))

    def apply(self, **kwargs):
        return apply_triage_result(
            repo=self.config.github_repo, issue_number=1, config=self.config,
            result=parse_triage_result(RESULT), github=self.github, issue_fields=self.fields,
            write_snapshot=self.snapshot, run_id="current", **kwargs,
        )

    def test_first_assignment_and_bot_owned_fields_apply_and_retry_converges(self) -> None:
        first = self.apply()
        second = self.apply()
        self.assertTrue(first.assignee_written)
        self.assertFalse(second.assignee_written)
        self.assertTrue(second.duplicate_comment_skipped)
        self.assertEqual(self.item.issue.assignees, ("bot-proposed-owner",))
        self.assertEqual(self.item.issue.title, "Corrected title")
        self.assertEqual(self.item.issue.issue_type, "Bug")
        self.assertEqual(self.item.fields["Priority"], "High")
        self.assertEqual(self.item.fields["Owner"], "suspected-introducer")
        self.assertEqual(self.item.fields["Owner reason"], "CODEOWNERS")
        self.assertEqual(self.item.fields["Triage status"], "Done")

    def test_manual_reassignment_or_aba_preserves_assignment_and_owner_fields_while_status_completes(self) -> None:
        for aba in (False, True):
            with self.subTest(aba=aba):
                self.setUp()
                self.item.issue = replace(self.item.issue, assignees=("human-owner",))
                self.item.record_write("assignee", "User")
                if aba:
                    self.item.issue = replace(self.item.issue, assignees=())
                    self.item.record_write("assignee", "User")
                summary = self.apply()
                self.assertFalse(summary.assignee_written)
                self.assertEqual(self.item.issue.assignees, () if aba else ("human-owner",))
                self.assertEqual(self.item.fields["Owner"], "original-introducer")
                self.assertEqual(self.item.fields["Owner reason"], "Original reason")
                self.assertEqual(self.item.fields["Triage status"], "Done")
                metadata = parse_triage_metadata(self.item.comments[-1])
                self.assertEqual(metadata["suggested_fields"]["assignee"], "bot-proposed-owner")
                self.assertEqual(metadata["suggested_fields"]["Owner reason"], "CODEOWNERS")

    def test_manual_title_and_priority_aba_block_only_their_own_writes(self) -> None:
        self.item.issue = replace(self.item.issue, title="Human title")
        self.item.record_write("title", "User")
        self.item.record_write("Priority", "User")
        self.item.record_write("Priority", "User")
        self.apply()
        self.assertEqual(self.item.issue.title, "Human title")
        self.assertEqual(self.item.fields["Priority"], "Low")
        self.assertEqual(self.item.issue.issue_type, "Bug")
        self.assertEqual(self.item.fields["Platform"], "Web")
        self.assertEqual(self.item.fields["Triage status"], "Done")

    def test_manual_owner_fields_remain_protected_even_when_first_assignment_is_allowed(self) -> None:
        for name in ("Owner", "Owner reason"):
            self.item.fields[name] = "Human decision"
            self.item.record_write(name, "User")
        self.apply()
        self.assertEqual(self.item.issue.assignees, ("bot-proposed-owner",))
        self.assertEqual(self.item.fields["Owner"], "Human decision")
        self.assertEqual(self.item.fields["Owner reason"], "Human decision")
        self.assertEqual(self.item.fields["Triage status"], "Done")

    def test_unknown_author_or_incomplete_history_only_suggests_business_changes(self) -> None:
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                self.setUp()
                if incomplete:
                    self.item.history_complete = False
                else:
                    for name in ("title", "issue_type", "Priority", "Platform", "assignee"):
                        self.item.record_write(name, "")
                self.apply()
                self.assertEqual(self.item.issue.title, "Initial title")
                self.assertEqual(self.item.issue.issue_type, "Task")
                self.assertEqual(self.item.issue.assignees, ())
                self.assertEqual(self.item.fields["Priority"], "Low")
                self.assertNotIn("Platform", self.item.fields)
                self.assertEqual(self.item.fields["Triage status"], "Done")
                self.assertIn("Suggestions:", self.item.comments[-1])

    def test_human_assignment_during_add_is_visible_and_never_removed(self) -> None:
        original_add = self.github.add_assignee

        def human_during_write(**kwargs):
            original_add(**kwargs)
            self.item.issue = replace(self.item.issue, assignees=(*self.item.issue.assignees, "human-owner"))
            self.item.assignees.append("human-owner")
            self.item.record_write("assignee", "User")

        self.github.add_assignee = human_during_write
        self.apply()
        self.assertEqual(self.item.issue.assignees, ("bot-proposed-owner", "human-owner"))
        self.assertEqual(self.item.fields["Owner reason"], "Original reason")
        self.assertIn("Assignment changed during writeback; review", self.item.comments[-1])
        self.assertIn("No assignee was removed", self.item.comments[-1])

    def test_newer_run_prevents_stale_system_status_and_business_writes(self) -> None:
        self.github.add_issue_comment(repo=self.config.github_repo, issue_number=1, body=append_triage_run_metadata({"run_id": "newer"}))
        with self.assertRaises(TriageWriteSuperseded):
            self.apply()
        self.assertEqual(self.item.fields["Triage status"], "Running")
        self.assertEqual(self.item.issue.assignees, ())

    def test_executor_finishes_running_after_manual_reassignment(self) -> None:
        self.item.comments.clear()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "triage-output.json"
            output.write_text(json.dumps(RESULT))
            plan = TriageRunPlan(Path(directory) / "context.md", Path(directory) / "schema.json", output,
                                 AgentInvocation(provider="codex", command=["fake-agent"], model="fake"), write_snapshot=self.snapshot)

            def complete(*args, **kwargs):
                self.item.issue = replace(self.item.issue, assignees=("human-owner",))
                self.item.record_write("assignee", "User")
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("bugpatrol.triage_runner.subprocess.run", side_effect=complete):
                status = execute_triage_run(config=self.config, issue_number=1, plan=plan, github=self.github, issue_fields=self.fields)
        self.assertEqual(status, "applied")
        self.assertEqual(self.item.issue.assignees, ("human-owner",))
        self.assertEqual(self.item.fields["Triage status"], "Done")
        self.assertEqual(self.item.fields["Owner reason"], "Original reason")

    def test_failed_old_executor_does_not_mark_new_run_failed_or_clear_its_inflight_claim(self) -> None:
        self.item.comments.clear()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "triage-output.json"
            plan = TriageRunPlan(Path(directory) / "context.md", Path(directory) / "schema.json", output,
                                 AgentInvocation(provider="codex", command=["fake-agent"]), write_snapshot=self.snapshot)

            def crash(*args, **kwargs):
                self.github.add_issue_comment(
                    repo=self.config.github_repo, issue_number=1,
                    body=append_triage_run_metadata({"run_id": "newer", "started_at": datetime.now(UTC).isoformat()}),
                )
                return subprocess.CompletedProcess([], 1, "", "crash")

            with patch("bugpatrol.triage_runner.subprocess.run", side_effect=crash):
                with self.assertRaisesRegex(RuntimeError, "agent failed"):
                    execute_triage_run(config=self.config, issue_number=1, plan=plan, github=self.github, issue_fields=self.fields)
        self.assertEqual(self.item.fields["Triage status"], "Running")
        self.assertIn("superseded", self.item.comments[-1])
        self.assertTrue(triage_run_in_flight(self.github.list_issue_comments(repo=self.config.github_repo, issue_number=1)))

    def test_prepared_plan_keeps_field_and_event_watermark_through_executor(self) -> None:
        config = replace(self.config, triage_agent=replace(self.config.triage_agent, provider="codex"))
        self.item.comments.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = prepare_triage_run(config=config, issue_number=1, repo_path=root,
                                      output_dir=root / "run", github=self.github, issue_fields=self.fields)
            self.assertEqual(plan.write_snapshot.fields["Priority"], "Low")
            self.assertTrue(plan.write_snapshot.history.complete)
            output = dict(RESULT, assignee=plan.known_assignees[0] if plan.known_assignees else RESULT["assignee"])
            plan.output_path.write_text(json.dumps(output))

            def complete(*args, **kwargs):
                self.item.record_write("assignee", "User")
                self.item.record_write("assignee", "User")
                self.item.record_write("Priority", "User")
                self.item.record_write("Priority", "User")
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("bugpatrol.triage_runner.subprocess.run", side_effect=complete):
                outcome = execute_triage_run(config=config, issue_number=1, plan=plan, github=self.github, issue_fields=self.fields)
        self.assertEqual(outcome, "applied")
        self.assertEqual(self.item.issue.assignees, ())
        self.assertEqual(self.item.fields["Priority"], "Low")
        self.assertEqual(self.item.fields["Triage status"], "Done")

    def test_owned_writeback_failure_reports_failed_instead_of_leaving_running(self) -> None:
        self.item.comments.clear()
        original_write = self.fields.add_issue_field_values

        def fail_business_write(**kwargs):
            if "Priority" in kwargs["values"]:
                raise RuntimeError("business field rejected")
            original_write(**kwargs)

        self.fields.add_issue_field_values = fail_business_write
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "triage-output.json"
            output.write_text(json.dumps(RESULT))
            plan = TriageRunPlan(Path(directory) / "context.md", Path(directory) / "schema.json", output,
                                 AgentInvocation(provider="codex", command=["fake-agent"]), write_snapshot=self.snapshot)
            with patch("bugpatrol.triage_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                with self.assertRaisesRegex(RuntimeError, "business field rejected"):
                    execute_triage_run(config=self.config, issue_number=1, plan=plan, github=self.github, issue_fields=self.fields)
        self.assertEqual(self.item.fields["Triage status"], "Failed")
        self.assertIn("Triage writeback failed", self.item.comments[-1])


if __name__ == "__main__":
    unittest.main()
