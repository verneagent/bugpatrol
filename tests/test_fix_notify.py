from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
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
    render_fix_notification_text,
    resolve_single_issue_from_pr,
)
from bugpatrol.lark import LarkOpenApiError
from bugpatrol.github import GitHubCliError
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

    def test_resend_redelivers_without_minting_a_second_marker(self) -> None:
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
        resend = apply_fix_notification(
            repo=config.github_repo,
            issue_number=issue.number,
            event="pr_merged",
            pr="123",
            dry_run=False,
            resend=True,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertTrue(first.lark_sent)
        self.assertTrue(first.metadata_written)
        # Re-delivered the Lark message, but did not skip and did not write a
        # second marker.
        self.assertTrue(resend.lark_sent)
        self.assertFalse(resend.duplicate_skipped)
        self.assertFalse(resend.metadata_written)
        # intake reply + two fix notifications = 3 Lark messages.
        self.assertEqual(len(lark.replies), 3)
        # Still exactly one marker comment.
        self.assertEqual(len(github.created[0].comments), 1)

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

    def test_reconcile_fix_notifications_isolates_a_gh_failure_per_candidate(self) -> None:
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
        intake_replies = len(lark.replies)
        # A transient gh/network failure on ONE issue must defer only that issue
        # (retried next pass — no marker is written) instead of aborting the
        # whole batch, so the other candidates still notify.
        bad_number = 999999
        original_get_issue = github.get_issue

        def get_issue(*, repo: str, issue_number: int):
            if issue_number == bad_number:
                raise GitHubCliError("gh api .../issues: EOF")
            return original_get_issue(repo=repo, issue_number=issue_number)

        github.get_issue = get_issue  # type: ignore[method-assign]

        result = reconcile_fix_notifications(
            repo=config.github_repo,
            candidates=(
                FixEventCandidate(event="issue_fixed", issue_number=issue.number),
                FixEventCandidate(event="issue_fixed", issue_number=bad_number),
            ),
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertEqual(result.attempted, 2)
        self.assertEqual(result.sent, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(len(lark.replies), intake_replies + 1)
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertTrue(any(str(bad_number) in error and "EOF" in error for error in result.errors))

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

    def _collector_fake(self):
        managed_body = '<!-- BUGPATROL_INTAKE_META:{"chat_id":"oc_1","root_id":"om_1"} -->'
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")

        class CollectorFake:
            def list_merged_pull_requests(self, *, repo: str, limit: int = 30):
                return (
                    GitHubPullRequest(
                        number=50,
                        url="https://github.test/o/r/pull/50",
                        title="Fix",
                        body="Closes #1",
                        closing_issue_numbers=(1,),
                        merged_at=recent,
                    ),
                    GitHubPullRequest(
                        number=51,
                        url="https://github.test/o/r/pull/51",
                        title="Fix",
                        body="Closes #9",
                        closing_issue_numbers=(9,),
                        merged_at=recent,
                    ),
                )

            def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
                body = "unmanaged" if issue_number == 9 else managed_body
                return GitHubIssue(
                    number=issue_number,
                    url=f"https://github.test/o/r/issues/{issue_number}",
                    title="bug",
                    body=body,
                )

            def list_issues(self, *, repo: str, state: str = "open"):
                return (
                    # #1: PR-covered -> its commit must be suppressed.
                    GitHubIssue(1, "https://github.test/o/r/issues/1", "bug", managed_body,
                                state="closed", state_reason="completed", closed_at=recent),
                    # #2: completed with a linked fix commit, no PR -> commit_linked.
                    GitHubIssue(2, "https://github.test/o/r/issues/2", "bug", managed_body,
                                state="closed", state_reason="completed", closed_at=recent),
                    # #3: not_planned -> no evidence, skipped even with a commit.
                    GitHubIssue(3, "https://github.test/o/r/issues/3", "bug", managed_body,
                                state="closed", state_reason="not_planned", closed_at=recent),
                    # #4: completed but closed long ago -> outside a recent window.
                    GitHubIssue(4, "https://github.test/o/r/issues/4", "bug", managed_body,
                                state="closed", state_reason="completed", closed_at=old),
                    # #9: unmanaged -> skipped.
                    GitHubIssue(9, "https://github.test/o/r/issues/9", "bug", "unmanaged",
                                state="closed", state_reason="completed", closed_at=recent),
                )

            def list_issue_timeline(self, *, repo: str, issue_number: int):
                commits = {1: "cafef00d", 2: "deadbeef", 3: "c0ffee00", 4: "0ldc0de0"}
                sha = commits.get(issue_number)
                return ({"event": "referenced", "commit_id": sha},) if sha else ()

        return CollectorFake()

    def test_collect_fix_candidates_evidence_gate(self) -> None:
        candidates = collect_fix_candidates_from_github(
            repo="o/r",
            github=self._collector_fake(),  # type: ignore[arg-type]
        )

        # PR-covered issue #1 -> pr_merged only; its timeline commit is suppressed.
        self.assertIn(FixEventCandidate(event="pr_merged", issue_number=1, pr="50"), candidates)
        self.assertFalse(
            any(c.event == "commit_linked" and c.issue_number == 1 for c in candidates)
        )
        # #2: completed + linked commit + no PR -> commit_linked.
        self.assertIn(
            FixEventCandidate(event="commit_linked", issue_number=2, commit="deadbeef"),
            candidates,
        )
        # #3 closed as not_planned carries no evidence -> nothing.
        self.assertFalse(any(c.issue_number == 3 for c in candidates))
        # Unmanaged #9 never appears; generic issue_fixed is no longer emitted.
        self.assertFalse(any(c.issue_number == 9 for c in candidates))
        self.assertFalse(any(c.event == "issue_fixed" for c in candidates))

    def test_collect_fix_candidates_skips_failed_pr_issue_lookup(self) -> None:
        class FlakyLookupFake:
            def list_merged_pull_requests(self, *, repo: str, limit: int = 30):
                recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
                return (
                    GitHubPullRequest(
                        number=50,
                        url="https://github.test/o/r/pull/50",
                        title="Fix",
                        body="Closes #1",
                        closing_issue_numbers=(1,),
                        merged_at=recent,
                    ),
                    GitHubPullRequest(
                        number=51,
                        url="https://github.test/o/r/pull/51",
                        title="Fix",
                        body="Closes #2",
                        closing_issue_numbers=(2,),
                        merged_at=recent,
                    ),
                )

            def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
                if issue_number == 1:
                    raise GitHubCliError("gh api .../issues/1: EOF")
                return GitHubIssue(
                    number=issue_number,
                    url=f"https://github.test/o/r/issues/{issue_number}",
                    title="bug",
                    body='<!-- BUGPATROL_INTAKE_META:{"chat_id":"oc_1","root_id":"om_1"} -->',
                )

            def list_issues(self, *, repo: str, state: str = "open"):
                return ()

        errors: list[str] = []
        candidates = collect_fix_candidates_from_github(
            repo="o/r",
            github=FlakyLookupFake(),  # type: ignore[arg-type]
            errors=errors,
        )

        self.assertEqual(candidates, (FixEventCandidate(event="pr_merged", issue_number=2, pr="51"),))
        self.assertTrue(any("issue #1" in error and "EOF" in error for error in errors))

    def test_collect_fix_candidates_skips_failed_timeline_lookup(self) -> None:
        class FlakyTimelineFake:
            def list_merged_pull_requests(self, *, repo: str, limit: int = 30):
                return ()

            def list_issues(self, *, repo: str, state: str = "open"):
                recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
                meta = '<!-- BUGPATROL_INTAKE_META:{"chat_id":"oc_1","root_id":"om_1"} -->'
                return (
                    GitHubIssue(
                        1,
                        "https://github.test/o/r/issues/1",
                        "bug",
                        meta,
                        state="closed",
                        state_reason="completed",
                        closed_at=recent,
                    ),
                    GitHubIssue(
                        2,
                        "https://github.test/o/r/issues/2",
                        "bug",
                        meta,
                        state="closed",
                        state_reason="completed",
                        closed_at=recent,
                    ),
                )

            def list_issue_timeline(self, *, repo: str, issue_number: int):
                if issue_number == 1:
                    raise GitHubCliError("gh api .../issues/1/timeline: EOF")
                return ({"event": "referenced", "commit_id": "deadbeef"},)

        errors: list[str] = []
        candidates = collect_fix_candidates_from_github(
            repo="o/r",
            github=FlakyTimelineFake(),  # type: ignore[arg-type]
            errors=errors,
        )

        self.assertEqual(candidates, (FixEventCandidate(event="commit_linked", issue_number=2, commit="deadbeef"),))
        self.assertTrue(any("issue #1 timeline" in error and "EOF" in error for error in errors))

    def test_collect_fix_candidates_since_days_window(self) -> None:
        candidates = collect_fix_candidates_from_github(
            repo="o/r",
            github=self._collector_fake(),  # type: ignore[arg-type]
            since_days=30,
        )
        # #2 (closed 2 days ago) is inside the window; #4 (100 days) is not.
        self.assertIn(
            FixEventCandidate(event="commit_linked", issue_number=2, commit="deadbeef"),
            candidates,
        )
        self.assertFalse(any(c.issue_number == 4 for c in candidates))

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

    def test_notification_text_uses_masked_short_links_and_commit_link(self) -> None:
        issue = GitHubIssue(
            number=3982,
            title="bug",
            body="",
            url="https://github.com/o/r/issues/3982",
            state="closed",
        )
        text = render_fix_notification_text(
            issue=issue, event="pr_merged", repo="o/r", pr="3983", commit="abc123def4567890"
        )
        # Masked markdown links: short label displayed, full URL only in href.
        # reply_to_message renders these as clickable Lark post links.
        self.assertIn("[#3983](https://github.com/o/r/pull/3983)", text)
        self.assertIn("[abc123def456](https://github.com/o/r/commit/abc123def4567890)", text)
        self.assertIn("[#3982](https://github.com/o/r/issues/3982)", text)
        # No bare full URL sitting in the visible text, no owner/repo#N shorthand.
        self.assertNotIn("：https://", text)
        self.assertNotIn("o/r#3983", text)
        # commit display is truncated to 12 chars.
        self.assertNotIn("abc123def4567890]", text)

    def test_notification_text_mentions_fix_author_and_reporter(self) -> None:
        issue = GitHubIssue(
            number=3982,
            title="bug",
            body="",
            url="https://github.com/o/r/issues/3982",
            state="closed",
        )
        text = render_fix_notification_text(
            issue=issue,
            event="pr_merged",
            repo="o/r",
            pr="3983",
            author_login="wind2star",
            author_open_id="ou_wind",
            reporter_open_id="ou_reporter",
        )
        # Reporter @-mentioned at the head, fix author @-mentioned on a 修复人 line.
        self.assertTrue(text.startswith('<at user_id="ou_reporter">上报人</at> '))
        self.assertIn('修复人：<at user_id="ou_wind">wind2star</at>', text)

    def test_notification_text_shows_bare_login_without_open_id_mapping(self) -> None:
        issue = GitHubIssue(number=1, title="b", body="", url="https://github.com/o/r/issues/1")
        text = render_fix_notification_text(
            issue=issue, event="commit_linked", repo="o/r", commit="abc123def4567890", author_login="ghost"
        )
        # No Lark open_id for this login -> plain text attribution, no <at> tag.
        self.assertIn("修复人：ghost", text)
        self.assertNotIn("<at", text)

    def test_issue_fixed_text_includes_pr_and_commit_when_provided(self) -> None:
        issue = GitHubIssue(number=7, title="b", body="", url="https://github.com/o/r/issues/7")
        text = render_fix_notification_text(
            issue=issue, event="issue_fixed", repo="o/r", pr="88", commit="deadbeef0000"
        )
        self.assertIn("该问题已标记修复", text)
        self.assertIn("[#88](https://github.com/o/r/pull/88)", text)
        self.assertIn("[deadbeef0000](https://github.com/o/r/commit/deadbeef0000)", text)

    def test_apply_fix_notification_resolves_pr_author_and_mentions(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        issue = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_reporter",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            )
        ).issue
        github.pull_requests["123"] = GitHubPullRequest(
            number=123, url="https://github.test/o/r/pull/123", title="", body="", author="wind2star"
        )

        apply_fix_notification(
            repo=config.github_repo,
            issue_number=issue.number,
            event="pr_merged",
            pr="123",
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
            user_open_ids={"wind2star": "ou_wind"},
        )

        text = lark.replies[-1].text
        self.assertIn('<at user_id="ou_reporter">上报人</at>', text)
        self.assertIn('修复人：<at user_id="ou_wind">wind2star</at>', text)

    def test_apply_fix_notification_resolves_commit_author(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        issue = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_reporter",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            )
        ).issue
        github.commit_authors["abc123def456"] = "jerry-emperor"

        apply_fix_notification(
            repo=config.github_repo,
            issue_number=issue.number,
            event="commit_linked",
            commit="abc123def456",
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
            user_open_ids={"jerry-emperor": "ou_jerry"},
        )

        self.assertIn('修复人：<at user_id="ou_jerry">jerry-emperor</at>', lark.replies[-1].text)

    def test_withdrawn_message_is_tolerated_and_still_records_marker(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = _WithdrawnLarkClient()
        workflow = IntakeWorkflow(
            config=config, github=github, lark=FakeLarkMessengerClient()
        )
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
            event="pr_merged",
            pr="123",
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,  # type: ignore[arg-type]
        )

        # Withdrawn Lark target: reply is not delivered, but the marker is still
        # written so the reconcile pass records it as handled and never retries.
        self.assertFalse(summary.lark_sent)
        self.assertTrue(summary.metadata_written)
        self.assertEqual(
            notified_fix_keys(
                github.list_issue_comments(repo=config.github_repo, issue_number=issue.number)
            ),
            {summary.key},
        )


class _WithdrawnLarkClient:
    def reply_to_message(self, *, chat_id: str, message_id: str, text: str) -> None:
        raise LarkOpenApiError('Lark HTTP 400: {"code":230011,"msg":"The message was withdrawn."}')


if __name__ == "__main__":
    unittest.main()
