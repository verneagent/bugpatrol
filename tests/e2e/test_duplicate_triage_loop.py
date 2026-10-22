from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient
from bugpatrol.triage_result import apply_triage_result, parse_triage_metadata, parse_triage_result


class FakeIssueFields:
    def __init__(self) -> None:
        self.values: dict[int, dict[str, str]] = {}

    def get_issue_field_values(self, *, issue_number: int, **kwargs: object) -> dict[str, str]:
        return dict(self.values.get(issue_number, {}))

    def add_issue_field_values(
        self, *, issue_number: int, values: dict[str, str], **kwargs: object
    ) -> None:
        self.values.setdefault(issue_number, {}).update(values)


class DuplicateTriageLoopTest(unittest.TestCase):
    def test_intake_triage_respects_delivery_evidence_and_keeps_unknown_reports_open(self) -> None:
        complete = json.loads(Path("tests/fixtures/duplicate_delivery.json").read_text())
        incomplete = copy.deepcopy(complete)
        incomplete["components"][1]["current_delivery"] = "missing"
        pre_fix = copy.deepcopy(complete)
        pre_fix["occurred_at"] = "2026-10-21T23:00:00Z"
        for component in pre_fix["components"]:
            component["reported_fix"] = "missing"
        old_build = copy.deepcopy(complete)
        old_build["components"][0]["reported_fix"] = "missing"
        old_build["components"][0]["build_predates_fix"] = True

        for evidence, classification, should_close, should_reopen in (
            (None, "needs_verification", False, False),
            (incomplete, "delivery_incomplete", False, False),
            (pre_fix, "pre_fix_duplicate", True, False),
            (complete, "confirmed_regression", True, True),
            (old_build, "old_build_duplicate", True, False),
        ):
            with self.subTest(classification=classification):
                config = load_project_config(Path("projects/todo-sandbox.toml"))
                github = FakeGitHubIssuesClient()
                lark = FakeLarkMessengerClient()
                workflow = IntakeWorkflow(config=config, github=github, lark=lark)
                for root in ("original", "new"):
                    workflow.process(IntakeRecord(
                        reporter_name="Reporter",
                        reporter_open_id="ou_reporter",
                        created_at="2026-10-22T03:00:00Z",
                        chat_id=config.lark.chat_id,
                        root_id=root,
                        message_id=root,
                        original_text="Todo deletion fails",
                    ))
                github.created[0].issue = replace(
                    github.created[0].issue, state="closed", state_reason="completed",
                    closed_at="2026-10-22T01:00:00Z",
                )
                result = parse_triage_result({
                    "issue_type": "Bug", "priority": "High", "triage_status": "Done",
                    "triage_verdict": "重复", "platform": "Web", "reproducibility": "必现",
                    "other_platforms": "未验证", "capability": "Quest", "evidence": "日志",
                    "prd_status": "已对齐", "triage_confidence": "高",
                    "assignee": "garlanddiego", "owner_reason": "CODEOWNERS",
                    "duplicate_of": 1, "duplicate_evidence": evidence,
                    "corrected_title": "Todo deletion fails",
                    "comment_markdown": "Same symptom as the original issue.",
                })
                fields = FakeIssueFields()
                first = apply_triage_result(
                    repo=config.github_repo, issue_number=2, config=config, result=result,
                    github=github, issue_fields=fields, lark=lark,
                )
                second = apply_triage_result(
                    repo=config.github_repo, issue_number=2, config=config, result=result,
                    github=github, issue_fields=fields, lark=lark,
                )
                self.assertEqual(first.closed_as_duplicate, should_close)
                self.assertEqual(github.created[1].closed_as_duplicate_of, 1 if should_close else 0)
                self.assertEqual(github.created[0].reopened, should_reopen)
                self.assertEqual(len(github.created[0].comments), 1 if should_reopen else 0)
                self.assertTrue(first.comment_added)
                self.assertFalse(second.comment_added)
                self.assertEqual(len(github.created[1].comments), 1)
                metadata = parse_triage_metadata(github.created[1].comments[0])
                self.assertEqual(metadata["duplicate_classification"], classification)
                self.assertEqual(metadata["duplicate_of"], 1 if should_close else 0)
                self.assertEqual(fields.values[2]["Triage status"], "Done")
                if not should_close:
                    self.assertFalse(any("已关闭" in reply.text for reply in lark.replies))
                    self.assertIn("保留", github.created[1].comments[0])


if __name__ == "__main__":
    unittest.main()
