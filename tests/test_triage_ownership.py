from __future__ import annotations

import unittest
from dataclasses import replace

from bugpatrol.clients import GitHubIssue, GitHubIssueComment
from bugpatrol.triage_ownership import (
    TriageWriteSnapshot,
    WriteEvent,
    WriteHistory,
    append_triage_run_metadata,
    assignment_block_reason,
    assignment_changed_by_human,
    field_block_reason,
    latest_triage_run_id,
)


class TriageOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.issue = GitHubIssue(1, "https://github.test/o/r/issues/1", "Initial", "", created_by_type="Bot")
        self.empty = WriteHistory(complete=True)
        self.snapshot = TriageWriteSnapshot(self.issue, {}, self.empty)

    def test_empty_first_assignment_requires_complete_history_and_snapshot(self) -> None:
        self.assertIsNone(assignment_block_reason(self.snapshot, self.issue, self.empty))
        self.assertIsNotNone(assignment_block_reason(None, self.issue, self.empty))
        self.assertIsNotNone(assignment_block_reason(self.snapshot, self.issue, WriteHistory()))

    def test_existing_assignees_are_preserved_even_when_proposal_matches(self) -> None:
        assigned = replace(self.issue, assignees=("owner",))
        self.assertIsNotNone(assignment_block_reason(self.snapshot, assigned, self.empty))
        self.assertIsNotNone(assignment_block_reason(replace(self.snapshot, issue=assigned), self.issue, self.empty))

    def test_human_assignment_aba_since_watermark_is_preserved(self) -> None:
        for actor in ("User", ""):
            history = WriteHistory((WriteEvent("a", "assignee", actor), WriteEvent("b", "assignee", actor)), True)
            self.assertIsNotNone(assignment_block_reason(self.snapshot, self.issue, history))
            self.assertTrue(assignment_changed_by_human(self.empty, history))

    def test_explicit_human_clear_before_plan_is_not_reassigned(self) -> None:
        history = WriteHistory((WriteEvent("a", "assignee", "User"),), True)
        self.assertIsNotNone(assignment_block_reason(replace(self.snapshot, history=history), self.issue, history))

    def test_field_snapshot_change_and_aba_are_both_protected(self) -> None:
        baseline = WriteHistory((WriteEvent("a", "Priority", "Bot"),), True)
        snapshot = replace(self.snapshot, fields={"Priority": "Low"}, history=baseline)
        self.assertIsNotNone(field_block_reason(snapshot, self.issue, {"Priority": "High"}, baseline, "Priority"))
        for field, value in (("Priority", "Low"), ("Empty", "")):
            history = WriteHistory((*baseline.events, WriteEvent("b", field, "User"), WriteEvent("c", field, "User")), True)
            self.assertIsNotNone(field_block_reason(snapshot, self.issue, {field: value}, history, field))

    def test_latest_editor_and_event_prefix_must_be_verifiable(self) -> None:
        for actor in ("User", ""):
            history = WriteHistory((WriteEvent("a", "Priority", actor),), True)
            snapshot = replace(self.snapshot, fields={"Priority": "Low"}, history=history)
            self.assertIsNotNone(field_block_reason(snapshot, self.issue, snapshot.fields, history, "Priority"))
        bot_history = WriteHistory((WriteEvent("a", "Priority", "Bot"),), True)
        snapshot = replace(self.snapshot, fields={"Priority": "Low"}, history=bot_history)
        self.assertIsNone(field_block_reason(snapshot, self.issue, snapshot.fields, bot_history, "Priority"))
        for history in (WriteHistory(), self.empty, WriteHistory((*bot_history.events, *bot_history.events), True)):
            self.assertIsNotNone(field_block_reason(snapshot, self.issue, snapshot.fields, history, "Priority"))

    def test_initial_bot_title_and_empty_fields_can_be_filled_without_inventing_authorship(self) -> None:
        self.assertIsNone(field_block_reason(self.snapshot, self.issue, {}, self.empty, "title"))
        self.assertIsNone(field_block_reason(self.snapshot, self.issue, {}, self.empty, "Priority"))
        human = replace(self.issue, created_by_type="User")
        self.assertIsNotNone(field_block_reason(replace(self.snapshot, issue=human), human, {}, self.empty, "title"))
        unknown = replace(self.snapshot, fields={"Priority": "Low"})
        self.assertIsNotNone(field_block_reason(unknown, self.issue, unknown.fields, self.empty, "Priority"))

    def test_only_trusted_bot_run_markers_claim_system_writes(self) -> None:
        comments = tuple(GitHubIssueComment(str(index), append_triage_run_metadata({"run_id": run}), author_type=actor)
                         for index, (actor, run) in enumerate((("Bot", "real"), ("User", "forged"), ("", "unknown"))))
        self.assertEqual(latest_triage_run_id(comments), "real")


if __name__ == "__main__":
    unittest.main()
