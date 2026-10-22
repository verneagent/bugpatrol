from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bugpatrol.__main__ import main
from bugpatrol.config import load_project_config
from bugpatrol.testing.fakes import (
    FakeGitHubIssueFieldsClient,
    FakeGitHubIssuesClient,
    FakeLarkMessengerClient,
)
from bugpatrol.triage_ownership import TriageWriteSnapshot
from bugpatrol.triage_result import apply_triage_result, parse_triage_metadata, parse_triage_result


class CliTriageLifecycleLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_project_config(Path("projects/todo-sandbox.toml"))
        self.github = FakeGitHubIssuesClient()
        self.fields = FakeGitHubIssueFieldsClient(self.github)
        self.lark = FakeLarkMessengerClient()
        for title in ("original", "report"):
            self.github.create_issue(
                repo=self.config.github_repo, title=title,
                body='<!-- BUGPATROL_INTAKE_META:{"source":"lark","chat_id":"test","root_id":"test","message_id":"test"} -->',
                issue_type="Bug", fields={"Triage status": "Done" if title == "original" else "Running"},
            )
        self.original, self.report = self.github.created
        self.original.issue = replace(self.original.issue, state="closed", state_reason="completed")
        self.evidence = json.loads(Path("tests/fixtures/duplicate_delivery.json").read_text())

    def result_data(self, evidence):
        return {
            "issue_type": "Bug", "priority": "High", "triage_status": "Done",
            "triage_verdict": "重复", "platform": "Web", "reproducibility": "必现",
            "other_platforms": "未验证", "capability": "Quest", "evidence": "日志",
            "prd_status": "已对齐", "triage_confidence": "高", "assignee": "garlanddiego",
            "owner_reason": "CODEOWNERS", "duplicate_of": 1, "duplicate_evidence": evidence,
            "corrected_title": "Report", "comment_markdown": "Same symptom as the original issue.",
        }

    def run_cli(self, evidence):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "result.json"
            source.write_text(json.dumps(self.result_data(evidence)))
            stdout = io.StringIO()
            with (
                patch("bugpatrol.__main__.load_project_config", return_value=self.config),
                patch("bugpatrol.__main__.GitHubCliIssuesClient", return_value=self.github),
                patch("bugpatrol.__main__.GitHubIssueFieldsClient", return_value=self.fields),
                patch("bugpatrol.__main__._optional_lark_client", return_value=self.lark),
                contextlib.redirect_stdout(stdout),
            ):
                code = main(["apply-triage-result", "projects/todo-sandbox.toml", "--issue", "2", "--input", str(source)])
        self.assertEqual(code, 0)
        return json.loads(stdout.getvalue())["summary"]

    def assert_suggestion_only(self, summary) -> None:
        self.assertFalse(summary["closed_as_duplicate"])
        self.assertEqual(summary["regression_reopened"], 0)
        metadata = parse_triage_metadata(self.report.comments[-1])
        self.assertEqual(metadata["duplicate_of"], 0)
        self.assertEqual(metadata["regression_of"], 0)
        self.assertIn("lifecycle", metadata["suggested_fields"])
        self.assertIn("No plan snapshot", self.report.comments[-1])
        self.assertFalse(any("将重新打开" in text or "已重新打开" in text or "已关闭" in text
                             for text in [self.report.comments[-1], *(reply.text for reply in self.lark.replies)]))
        self.assertTrue(any("Lifecycle suggestion only" in reply.text for reply in self.lark.replies))

    def test_cli_without_snapshot_cannot_set_original_pending_reopen_or_flag_regression(self) -> None:
        summary = self.run_cli(self.evidence)
        self.assertEqual(self.original.issue.state, "closed")
        self.assertEqual(self.original.fields["Triage status"], "Done")
        self.assertEqual(self.original.comments, [])
        self.assertEqual(self.report.issue.state, "open")
        self.assertEqual(self.report.closed_as_duplicate_of, 0)
        self.assertEqual(self.report.fields["Triage status"], "Done")
        self.assert_suggestion_only(summary)

    def test_cli_without_snapshot_cannot_close_duplicate_or_write_close_receipts(self) -> None:
        self.evidence["components"][0]["reported_fix"] = "missing"
        self.evidence["components"][0]["build_predates_fix"] = True
        summary = self.run_cli(self.evidence)
        self.assertEqual(self.report.issue.state, "open")
        self.assertFalse(any("BUGPATROL_DUPLICATE_CLOSE" in body for body in self.report.comments))
        self.assertEqual(self.report.fields["Triage status"], "Done")
        self.assert_suggestion_only(summary)

    def test_cli_without_snapshot_cannot_restore_bot_closed_report_even_with_matching_receipt(self) -> None:
        self.evidence["components"][0]["reported_fix"] = "missing"
        self.evidence["components"][0]["build_predates_fix"] = True
        snapshot = TriageWriteSnapshot(self.report.issue, dict(self.report.fields),
                                      self.github.get_issue_write_history(repo=self.config.github_repo, issue_number=2))
        prepared = apply_triage_result(repo=self.config.github_repo, issue_number=2, config=self.config,
                                       result=parse_triage_result(self.result_data(self.evidence)), github=self.github,
                                       issue_fields=self.fields, write_snapshot=snapshot)
        self.assertTrue(prepared.closed_as_duplicate)
        self.assertTrue(any("BUGPATROL_DUPLICATE_CLOSE_META" in body for body in self.report.comments))
        summary = self.run_cli(None)
        self.assertEqual(self.report.issue.state, "closed")
        self.assertFalse(self.report.reopened)
        self.assertEqual(self.report.fields["Triage status"], "Needs info")
        self.assert_suggestion_only(summary)


if __name__ == "__main__":
    unittest.main()
