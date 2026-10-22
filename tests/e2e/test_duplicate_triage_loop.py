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
from bugpatrol.triage_ownership import TriageWriteSnapshot
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
    def snapshot(self, github, fields, config):
        return TriageWriteSnapshot(
            github.get_issue(repo=config.github_repo, issue_number=2),
            fields.get_issue_field_values(repo=config.github_repo, issue_number=2),
            github.get_issue_write_history(repo=config.github_repo, issue_number=2),
        )

    def result(self, evidence: dict | None):
        return parse_triage_result({
            "issue_type": "Bug", "priority": "High", "triage_status": "Done",
            "triage_verdict": "重复", "platform": "Web", "reproducibility": "必现",
            "other_platforms": "未验证", "capability": "Quest", "evidence": "日志",
            "prd_status": "已对齐", "triage_confidence": "高",
            "assignee": "garlanddiego", "owner_reason": "CODEOWNERS",
            "duplicate_of": 1, "duplicate_evidence": evidence,
            "corrected_title": "Todo deletion fails",
            "comment_markdown": "Same symptom as the original issue.",
        })

    def test_receipt_committed_before_timeout_is_read_back_without_reopening(self) -> None:
        class PersistReceiptThenTimeout(FakeGitHubIssuesClient):
            receipt_timed_out = False
            readback_failed = False
            fail_readback = False

            def add_issue_comment(self, **kwargs):
                super().add_issue_comment(**kwargs)
                if "BUGPATROL_DUPLICATE_CLOSE_META" in kwargs["body"] and not self.receipt_timed_out:
                    self.receipt_timed_out = True
                    raise RuntimeError("receipt response timed out")

            def list_issue_comments(self, **kwargs):
                if self.fail_readback and self.receipt_timed_out and not self.readback_failed:
                    self.readback_failed = True
                    raise RuntimeError("receipt readback unavailable")
                return super().list_issue_comments(**kwargs)

        evidence = json.loads(Path("tests/fixtures/duplicate_delivery.json").read_text())
        evidence["components"][0]["reported_fix"] = "missing"
        evidence["components"][0]["build_predates_fix"] = True
        for fail_readback in (False, True):
            with self.subTest(fail_readback=fail_readback):
                config = load_project_config(Path("projects/todo-sandbox.toml"))
                github = PersistReceiptThenTimeout()
                github.fail_readback = fail_readback
                for title in ("original", "new"):
                    github.create_issue(
                        repo=config.github_repo, title=title,
                        body='<!-- BUGPATROL_INTAKE_META:{"source":"lark","chat_id":"oc_x","root_id":"om_x"} -->',
                        issue_type="Bug", fields={},
                    )
                github.created[0].issue = replace(github.created[0].issue, state="closed", state_reason="completed")
                arguments = dict(repo=config.github_repo, issue_number=2, config=config,
                                 github=github, issue_fields=FakeIssueFields(), result=self.result(evidence))
                arguments["write_snapshot"] = self.snapshot(github, arguments["issue_fields"], config)
                if fail_readback:
                    with self.assertRaisesRegex(RuntimeError, "readback unavailable"):
                        apply_triage_result(**arguments)
                else:
                    self.assertTrue(apply_triage_result(**arguments).closed_as_duplicate)
                self.assertEqual(github.created[1].issue.state, "closed")
                self.assertFalse(github.created[1].reopened)
                self.assertTrue(apply_triage_result(**arguments).closed_as_duplicate)
                self.assertEqual(sum("BUGPATROL_DUPLICATE_CLOSE_META" in body for body in github.created[1].comments), 1)

    def test_receipt_write_failure_and_failed_compensation_recover_without_overwriting_human_close(self) -> None:
        class FailReceiptAndReopenOnce(FakeGitHubIssuesClient):
            receipt_failed = False
            reopen_failed = False
            human_during_receipt = False

            def add_issue_comment(self, **kwargs):
                if "BUGPATROL_DUPLICATE_CLOSE_META" in kwargs["body"] and not self.receipt_failed:
                    self.receipt_failed = True
                    if self.human_during_receipt:
                        self.created[1].issue = replace(
                            self.created[1].issue, closed_by="human", closed_by_type="User",
                            closed_at="2026-10-22T04:00:00Z",
                        )
                    raise RuntimeError("receipt write failed")
                super().add_issue_comment(**kwargs)

            def reopen_issue(self, **kwargs):
                if not self.reopen_failed:
                    self.reopen_failed = True
                    raise RuntimeError("compensation failed")
                super().reopen_issue(**kwargs)

        complete = json.loads(Path("tests/fixtures/duplicate_delivery.json").read_text())
        old_build = copy.deepcopy(complete)
        old_build["components"][0]["reported_fix"] = "missing"
        old_build["components"][0]["build_predates_fix"] = True
        scenarios = [(human, same) for human in ("none", "before_retry", "during_receipt") for same in (False, True)]
        for human_close, same_evidence_retry in scenarios:
            with self.subTest(human_close=human_close, same_evidence_retry=same_evidence_retry):
                config = load_project_config(Path("projects/todo-sandbox.toml"))
                github = FailReceiptAndReopenOnce()
                github.human_during_receipt = human_close == "during_receipt"
                for title in ("original", "new"):
                    github.create_issue(
                        repo=config.github_repo, title=title,
                        body='<!-- BUGPATROL_INTAKE_META:{"source":"lark","chat_id":"oc_x","root_id":"om_x"} -->',
                        issue_type="Bug", fields={},
                    )
                github.created[0].issue = replace(github.created[0].issue, state="closed", state_reason="completed")
                fields = FakeIssueFields()
                arguments = dict(repo=config.github_repo, issue_number=2, config=config,
                                 github=github, issue_fields=fields, write_snapshot=self.snapshot(github, fields, config))
                with self.assertRaises(RuntimeError):
                    apply_triage_result(**arguments, result=self.result(old_build))
                self.assertEqual(github.reopen_failed, human_close != "during_receipt")
                self.assertEqual(github.created[1].issue.state, "closed")
                if human_close == "before_retry":
                    github.created[1].issue = replace(
                        github.created[1].issue, closed_by="human", closed_by_type="User",
                        closed_at="2026-10-22T04:00:00Z",
                    )
                if same_evidence_retry:
                    apply_triage_result(**arguments, result=self.result(old_build))
                    self.assertEqual(github.created[1].issue.state, "closed")
                    if human_close == "none":
                        self.assertTrue(any("BUGPATROL_DUPLICATE_CLOSE_META" in body for body in github.created[1].comments))
                apply_triage_result(**arguments, result=self.result(None))
                self.assertEqual(github.created[1].issue.state, "open" if human_close == "none" else "closed")
                self.assertEqual(fields.values[2]["Triage status"], "Needs info")

    def test_bot_duplicate_close_is_reopened_when_later_evidence_requires_tracking(self) -> None:
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
        for prior in (pre_fix, old_build):
            for later in (None, incomplete):
                for provenance in ("bot", "human", "no_receipt", "later_bot_close", "untrusted_receipt"):
                    with self.subTest(prior=prior["occurred_at"], later=later is None, provenance=provenance):
                        config = load_project_config(Path("projects/todo-sandbox.toml"))
                        github = FakeGitHubIssuesClient()
                        for title in ("original", "new"):
                            github.create_issue(
                                repo=config.github_repo, title=title,
                                body='<!-- BUGPATROL_INTAKE_META:{"source":"lark","chat_id":"oc_x","root_id":"om_x"} -->',
                                issue_type="Bug", fields={},
                            )
                        github.created[0].issue = replace(
                            github.created[0].issue, state="closed", state_reason="completed",
                        )
                        fields = FakeIssueFields()
                        arguments = dict(repo=config.github_repo, issue_number=2, config=config,
                                         github=github, issue_fields=fields, write_snapshot=self.snapshot(github, fields, config))
                        apply_triage_result(**arguments, result=self.result(prior))
                        self.assertEqual(github.created[1].issue.state, "closed")
                        if provenance == "human":
                            github.created[1].issue = replace(
                                github.created[1].issue, closed_by="human",
                                closed_by_type="User",
                                closed_at="2026-10-22T04:00:00Z",
                            )
                        elif provenance == "no_receipt":
                            github.created[1].comments = [
                                body for body in github.created[1].comments
                                if "BUGPATROL_DUPLICATE_CLOSE_META" not in body
                            ]
                        elif provenance == "later_bot_close":
                            github.created[1].issue = replace(
                                github.created[1].issue, closed_at="2026-10-22T04:00:00Z",
                            )
                        elif provenance == "untrusted_receipt":
                            read_comments = github.list_issue_comments

                            def untrusted_comments(_read_comments=read_comments, **kwargs):
                                return tuple(
                                    replace(comment, author="reporter", author_type="User")
                                    if "BUGPATROL_DUPLICATE_CLOSE" in comment.body else comment
                                    for comment in _read_comments(**kwargs)
                                )

                            github.list_issue_comments = untrusted_comments
                        apply_triage_result(**arguments, result=self.result(later))
                        self.assertEqual(github.created[1].issue.state, "open" if provenance in ("bot", "no_receipt") else "closed")
                        self.assertEqual(fields.values[2]["Triage status"], "Needs info")

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
                snapshot = self.snapshot(github, fields, config)
                first = apply_triage_result(
                    repo=config.github_repo, issue_number=2, config=config, result=result,
                    github=github, issue_fields=fields, lark=lark, write_snapshot=snapshot,
                )
                second = apply_triage_result(
                    repo=config.github_repo, issue_number=2, config=config, result=result,
                    github=github, issue_fields=fields, lark=lark, write_snapshot=snapshot,
                )
                self.assertEqual(first.closed_as_duplicate, should_close)
                self.assertEqual(github.created[1].closed_as_duplicate_of, 1 if should_close else 0)
                self.assertEqual(github.created[0].reopened, should_reopen)
                self.assertEqual(len(github.created[0].comments), 1 if should_reopen else 0)
                self.assertTrue(first.comment_added)
                self.assertFalse(second.comment_added)
                self.assertEqual(len(github.created[1].comments), 3 if should_close else 1)
                metadata = parse_triage_metadata(github.created[1].comments[0])
                self.assertEqual(metadata["duplicate_classification"], classification)
                self.assertEqual(metadata["duplicate_of"], 1 if should_close else 0)
                self.assertEqual(fields.values[2]["Triage status"], "Done" if should_close else "Needs info")
                if not should_close:
                    self.assertFalse(any("已关闭" in reply.text for reply in lark.replies))
                    self.assertIn("保留", github.created[1].comments[0])


if __name__ == "__main__":
    unittest.main()
