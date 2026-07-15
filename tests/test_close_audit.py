from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from bugpatrol.clients import GitHubIssue, GitHubIssueComment
from bugpatrol.close_audit import (
    audit_issue_close,
    fix_evidence_for_issue,
    parse_close_audit_metadata,
    render_close_audit_metadata_comment,
)
from bugpatrol.config import load_project_config
from bugpatrol.fix_notify import render_fix_metadata_comment
from bugpatrol.intake import IntakeRecord, render_issue_body
from bugpatrol.testing.fakes import FakeLarkMessengerClient
from bugpatrol.triage_result import append_triage_metadata


def render_triage_metadata_comment(*, duplicate_of: int) -> str:
    return append_triage_metadata(
        "结论：重复，已关闭。",
        {"version": 1, "issue": 7, "duplicate_of": duplicate_of},
    )


def render_expected_behavior_triage_comment() -> str:
    return append_triage_metadata(
        "结论：预期行为，已关闭。",
        {"version": 1, "issue": 7, "duplicate_of": 0, "verdict": "预期行为"},
    )


def _managed_body(chat_id: str = "oc_1") -> str:
    return render_issue_body(
        IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id=chat_id,
            root_id="om_root",
            message_id="om_1",
            original_text="bug",
        ),
        language="zh-CN",
    )


class FakeGithub:
    def __init__(self, *, issue: GitHubIssue, timeline: tuple[dict, ...] = ()) -> None:
        self.issue = issue
        self.timeline = timeline
        self.comments: list[str] = []

    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        return self.issue

    def list_issue_comments(self, *, repo: str, issue_number: int) -> tuple[GitHubIssueComment, ...]:
        return tuple(
            GitHubIssueComment(id=str(index + 1), body=body)
            for index, body in enumerate(self.comments)
        )

    def list_issue_timeline(self, *, repo: str, issue_number: int) -> tuple[dict, ...]:
        return self.timeline

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        self.comments.append(body)


def _closed_issue(**overrides) -> GitHubIssue:
    values = dict(
        number=7,
        url="https://github.test/o/r/issues/7",
        title="bug",
        body=_managed_body(),
        state="closed",
        state_reason="completed",
        closed_by="octocat",
        assignees=("garlanddiego",),
    )
    values.update(overrides)
    return GitHubIssue(**values)


class CloseAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_project_config(Path("projects/todo-sandbox.toml"))

    def test_metadata_round_trips(self) -> None:
        body = render_close_audit_metadata_comment({"version": 1, "issue": 7})

        self.assertEqual(parse_close_audit_metadata(body), {"version": 1, "issue": 7})
        self.assertIsNone(parse_close_audit_metadata("plain comment"))

    def test_evidence_from_timeline_close_commit(self) -> None:
        evidence = fix_evidence_for_issue(
            timeline=({"event": "closed", "commit_id": "abc123"},),
            comments=(),
        )

        self.assertEqual(evidence, "commit abc123")

    def test_evidence_from_merged_pr_cross_reference(self) -> None:
        evidence = fix_evidence_for_issue(
            timeline=(
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 12,
                            "pull_request": {"merged_at": "2026-07-08T00:00:00Z"},
                        }
                    },
                },
            ),
            comments=(),
        )

        self.assertEqual(evidence, "merged PR #12")

    def test_unmerged_pr_cross_reference_is_not_evidence(self) -> None:
        evidence = fix_evidence_for_issue(
            timeline=(
                {
                    "event": "cross-referenced",
                    "source": {"issue": {"number": 12, "pull_request": {"merged_at": None}}},
                },
            ),
            comments=(),
        )

        self.assertEqual(evidence, "")

    def test_evidence_from_fix_notification_metadata(self) -> None:
        comment = render_fix_metadata_comment({"version": 1, "key": "pr_merged:o/r#5", "pr": "#5"})

        evidence = fix_evidence_for_issue(
            timeline=(),
            comments=(GitHubIssueComment(id="1", body=comment),),
        )

        self.assertEqual(evidence, "fix notification PR #5")

    def test_skips_unmanaged_issue(self) -> None:
        github = FakeGithub(issue=_closed_issue(body="plain issue"))

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=self.config, github=github, dry_run=False
        )

        self.assertFalse(summary.audited)
        self.assertEqual(summary.skipped_reason, "not bugpatrol-managed")

    def test_skips_unknown_close_reason(self) -> None:
        github = FakeGithub(issue=_closed_issue(state_reason=""))

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=self.config, github=github, dry_run=False
        )

        self.assertFalse(summary.audited)
        self.assertIn("nothing to notify", summary.skipped_reason)
        self.assertEqual(github.comments, [])

    def _notify_config(self):
        return dataclasses.replace(
            self.config,
            lark=dataclasses.replace(self.config.lark, user_open_ids={"garlanddiego": "ou_dev"}),
        )

    def test_notifies_and_dedupes_on_not_planned_close(self) -> None:
        config = self._notify_config()
        github = FakeGithub(
            issue=_closed_issue(state_reason="not_planned", body=_managed_body(chat_id=config.lark.chat_id))
        )
        lark = FakeLarkMessengerClient()

        first = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )
        second = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(first.notified)
        self.assertTrue(first.lark_sent)
        self.assertFalse(second.notified)
        self.assertEqual(second.skipped_reason, "already notified")
        self.assertEqual(len(github.comments), 1)
        self.assertEqual(len(lark.replies), 1)
        text = lark.replies[0].text
        self.assertIn("not planned", text)
        self.assertIn("octocat（GitHub）", text)
        # reporter (ou_1 from _managed_body) and assignee are both @-mentioned.
        self.assertIn('<at user_id="ou_1">上报人</at>', text)
        self.assertIn('<at user_id="ou_dev">garlanddiego</at>', text)

    def test_notifies_on_duplicate_close(self) -> None:
        config = self._notify_config()
        github = FakeGithub(
            issue=_closed_issue(state_reason="duplicate", body=_managed_body(chat_id=config.lark.chat_id))
        )
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.notified)
        self.assertEqual(summary.kind, "closed_duplicate")
        self.assertIn("duplicate", lark.replies[0].text)

    def test_skips_duplicate_when_triage_already_announced(self) -> None:
        config = self._notify_config()
        github = FakeGithub(
            issue=_closed_issue(state_reason="duplicate", body=_managed_body(chat_id=config.lark.chat_id))
        )
        github.comments.append(render_triage_metadata_comment(duplicate_of=3))

        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.audited)
        self.assertFalse(summary.notified)
        self.assertEqual(summary.skipped_reason, "triage already announced duplicate")
        self.assertEqual(len(lark.replies), 0)

    def test_skips_not_planned_when_triage_announced_expected_behavior(self) -> None:
        config = self._notify_config()
        github = FakeGithub(
            issue=_closed_issue(state_reason="not_planned", body=_managed_body(chat_id=config.lark.chat_id))
        )
        github.comments.append(render_expected_behavior_triage_comment())
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.audited)
        self.assertFalse(summary.notified)
        self.assertEqual(summary.skipped_reason, "triage already announced expected behavior")
        self.assertEqual(len(lark.replies), 0)

    def test_passes_when_evidence_exists(self) -> None:
        github = FakeGithub(
            issue=_closed_issue(),
            timeline=({"event": "closed", "commit_id": "abc123"},),
        )

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=self.config, github=github, dry_run=False
        )

        self.assertTrue(summary.audited)
        self.assertEqual(summary.evidence, "commit abc123")
        self.assertFalse(summary.nagged)
        self.assertEqual(github.comments, [])

    def test_nags_once_with_github_comment_and_lark_mention(self) -> None:
        config = dataclasses.replace(
            self.config,
            lark=dataclasses.replace(
                self.config.lark, user_open_ids={"garlanddiego": "ou_dev"}
            ),
        )
        github = FakeGithub(issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)))
        lark = FakeLarkMessengerClient()

        first = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )
        second = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(first.nagged)
        self.assertTrue(first.lark_sent)
        self.assertFalse(second.nagged)
        self.assertEqual(second.skipped_reason, "already notified")
        self.assertEqual(len(github.comments), 1)
        self.assertIn("@garlanddiego", github.comments[0])
        self.assertIn("Fixes #7", github.comments[0])
        self.assertEqual(len(lark.replies), 1)
        self.assertIn('<at user_id="ou_dev">garlanddiego</at>', lark.replies[0].text)

    def test_dry_run_does_not_comment(self) -> None:
        github = FakeGithub(issue=_closed_issue())

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=self.config, github=github, dry_run=True
        )

        self.assertTrue(summary.audited)
        self.assertFalse(summary.nagged)
        self.assertEqual(github.comments, [])


if __name__ == "__main__":
    unittest.main()
