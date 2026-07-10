from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.clients import GitHubIssue, GitHubPullRequest
from bugpatrol.fix_notify import (
    FixEventCandidate,
    apply_fix_notification,
    associated_issue_numbers_from_pr,
    collect_fix_candidates_from_github,
    fix_event_candidates_from_json,
    fix_notification_key,
    issue_numbers_from_timeline_events,
    linked_commits_from_timeline,
    notified_fix_keys,
    parse_fix_metadata,
    reconcile_fix_notifications,
    render_fix_metadata_comment,
    resolve_single_issue_from_pr,
)
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient


class FixNotifyTest(unittest.TestCase):
    def test_fix_notification_key_uses_stable_event_keys(self) -> None:
        self.assertEqual(
            fix_notification_key(repo="o/r", issue_number=1, event="pr_opened", pr="#2"),
            "pr_opened:o/r#2",
        )
        self.assertEqual(
            fix_notification_key(repo="o/r", issue_number=1, event="commit_linked", commit="abc"),
            "commit:o/r@abc",
        )
        self.assertEqual(
            fix_notification_key(repo="o/r", issue_number=1, event="issue_fixed"),
            "issue_fixed:o/r#1",
        )

    def test_fix_metadata_round_trips(self) -> None:
        body = render_fix_metadata_comment({"version": 1, "key": "pr_merged:o/r#2"})

        self.assertEqual(parse_fix_metadata(body), {"version": 1, "key": "pr_merged:o/r#2"})

    def test_associated_issue_numbers_from_pr_uses_closing_refs_title_and_body(self) -> None:
        pr = GitHubPullRequest(
            number=9,
            url="https://github.test/o/r/pull/9",
            title="Fix #3",
            body="Closes #2\nRefs o/r#4\nHex abc#123 is not a repo ref.",
            closing_issue_numbers=(1, 2),
            timeline_issue_numbers=(5,),
        )

        self.assertEqual(associated_issue_numbers_from_pr(pr), (1, 2, 3, 4, 5))

    def test_issue_numbers_from_timeline_events_reads_source_subject_and_issue(self) -> None:
        events = [
            {"event": "cross-referenced", "source": {"type": "issue", "issue": {"number": 2}}},
            {"event": "connected", "subject": {"type": "issue", "number": 3}},
            {"event": "referenced", "issue": {"number": 4}},
            {"event": "self", "issue": {"number": 9}},
        ]

        self.assertEqual(issue_numbers_from_timeline_events(events, exclude=(9,)), (2, 3, 4))

    def test_resolve_single_issue_from_pr_requires_exactly_one_issue(self) -> None:
        self.assertEqual(
            resolve_single_issue_from_pr(
                GitHubPullRequest(
                    number=9,
                    url="https://github.test/o/r/pull/9",
                    title="Fix",
                    body="Closes #2",
                )
            ),
            2,
        )
        with self.assertRaisesRegex(ValueError, "exactly one issue"):
            resolve_single_issue_from_pr(
                GitHubPullRequest(
                    number=9,
                    url="https://github.test/o/r/pull/9",
                    title="Fix",
                    body="Closes #2 and #3",
                )
            )

    def test_notify_fix_write_sends_once_and_records_metadata(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        issue = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_1",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            )
        ).issue

        first = apply_fix_notification(
            repo=config.github_repo,
            issue_number=issue.number,
            event="pr_merged",
            pr="123",
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )
        second = apply_fix_notification(
            repo=config.github_repo,
            issue_number=issue.number,
            event="pr_merged",
            pr="123",
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertTrue(first.lark_sent)
        self.assertFalse(first.duplicate_skipped)
        self.assertFalse(second.lark_sent)
        self.assertTrue(second.duplicate_skipped)
        self.assertEqual(len(lark.replies), 2)
        self.assertIn("已创建 GitHub issue", lark.replies[0].text)
        self.assertIn("修复 PR 已合并", lark.replies[1].text)
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertEqual(notified_fix_keys(github.list_issue_comments(repo=config.github_repo, issue_number=1)), {first.key})

    def test_reconcile_fix_notifications_dedupes_multiple_workflow_reruns(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        issue = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_1",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            )
        ).issue

        result = reconcile_fix_notifications(
            repo=config.github_repo,
            candidates=(
                FixEventCandidate(event="pr_opened", issue_number=issue.number, pr="123"),
                FixEventCandidate(event="pr_opened", issue_number=issue.number, pr="123"),
                FixEventCandidate(event="pr_opened", issue_number=issue.number, pr="123"),
            ),
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertEqual(result.attempted, 3)
        self.assertEqual(result.sent, 1)
        self.assertEqual(result.duplicate_skipped, 2)
        self.assertEqual(len(lark.replies), 2)
        self.assertEqual(len(github.created[0].comments), 1)

    def test_reconcile_fix_notifications_skips_unmanaged_issue(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        issue = github.create_issue(
            repo=config.github_repo,
            title="Legacy issue",
            body="created before BugPatrol",
            issue_type="Bug",
            fields={},
        )

        result = reconcile_fix_notifications(
            repo=config.github_repo,
            candidates=(FixEventCandidate(event="pr_opened", issue_number=issue.number, pr="123"),),
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.skipped, 1)
        self.assertIn("no BugPatrol Lark intake metadata", result.errors[0])
        self.assertEqual(lark.replies, [])
        self.assertEqual(github.created[0].comments, [])

    def test_reconcile_fix_notifications_resolves_issue_from_pr_timeline(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        issue = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_1",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            )
        ).issue
        github.get_pull_request = lambda repo, pr: GitHubPullRequest(  # type: ignore[method-assign]
            number=int(pr),
            url=f"https://github.test/{repo}/pull/{pr}",
            title="Fix",
            body="",
            timeline_issue_numbers=(issue.number,),
        )

        result = reconcile_fix_notifications(
            repo=config.github_repo,
            candidates=(FixEventCandidate(event="pr_merged", pr="123"),),
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertEqual(result.sent, 1)
        self.assertEqual(result.errors, ())

    def test_fix_event_candidates_from_json_accepts_issue_alias(self) -> None:
        candidates = fix_event_candidates_from_json(
            [{"event": "commit_linked", "issue": "7", "commit": "abc"}]
        )

        self.assertEqual(
            candidates,
            (FixEventCandidate(event="commit_linked", issue_number=7, commit="abc"),),
        )

    def test_linked_commits_from_timeline_dedupes_fix_events(self) -> None:
        events = [
            {"event": "referenced", "commit_id": "abc"},
            {"event": "closed", "commit_id": "def"},
            {"event": "referenced", "commit_id": "abc"},
            {"event": "labeled", "commit_id": "ghi"},
            {"event": "closed", "commit_id": None},
        ]

        self.assertEqual(linked_commits_from_timeline(events), ("abc", "def"))

    def test_collect_fix_candidates_from_github_gathers_managed_only(self) -> None:
        managed_body = '<!-- BUGPATROL_INTAKE_META:{"chat_id":"oc_1","root_id":"om_1"} -->'

        class CollectorFake:
            def list_merged_pull_requests(self, *, repo: str, limit: int = 30):
                return (
                    GitHubPullRequest(
                        number=50,
                        url="https://github.test/o/r/pull/50",
                        title="Fix",
                        body="Closes #1",
                        closing_issue_numbers=(1,),
                    ),
                    GitHubPullRequest(
                        number=51,
                        url="https://github.test/o/r/pull/51",
                        title="Fix",
                        body="Closes #9",
                        closing_issue_numbers=(9,),
                    ),
                )

            def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
                body = managed_body if issue_number == 1 else "unmanaged"
                return GitHubIssue(
                    number=issue_number,
                    url=f"https://github.test/o/r/issues/{issue_number}",
                    title="bug",
                    body=body,
                )

            def list_issues(self, *, repo: str, state: str = "open"):
                return (
                    GitHubIssue(
                        number=1,
                        url="https://github.test/o/r/issues/1",
                        title="bug",
                        body=managed_body,
                    ),
                    GitHubIssue(
                        number=9,
                        url="https://github.test/o/r/issues/9",
                        title="bug",
                        body="unmanaged",
                    ),
                )

            def list_issue_timeline(self, *, repo: str, issue_number: int):
                if issue_number == 1:
                    return ({"event": "referenced", "commit_id": "cafef00d"},)
                return ()

        candidates = collect_fix_candidates_from_github(
            repo="o/r",
            github=CollectorFake(),  # type: ignore[arg-type]
        )

        self.assertIn(
            FixEventCandidate(event="pr_merged", issue_number=1, pr="50"), candidates
        )
        self.assertIn(FixEventCandidate(event="issue_fixed", issue_number=1), candidates)
        self.assertIn(
            FixEventCandidate(event="commit_linked", issue_number=1, commit="cafef00d"),
            candidates,
        )
        # Unmanaged issue #9 must not appear in any form.
        self.assertFalse(any(c.issue_number == 9 for c in candidates))

    def test_notify_fix_dry_run_does_not_send_or_write(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        issue = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_1",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            )
        ).issue

        summary = apply_fix_notification(
            repo=config.github_repo,
            issue_number=issue.number,
            event="issue_fixed",
            dry_run=True,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertTrue(summary.dry_run)
        self.assertFalse(summary.lark_sent)
        self.assertEqual(len(lark.replies), 1)
        self.assertEqual(github.created[0].comments, [])


if __name__ == "__main__":
    unittest.main()
