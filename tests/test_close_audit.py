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
from bugpatrol.config import ReferenceRepo, load_project_config
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


def _managed_body(chat_id: str = "oc_1", target_branch: str = "main") -> str:
    return render_issue_body(
        IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id=chat_id,
            root_id="om_root",
            message_id="om_1",
            original_text="bug",
            target_branch=target_branch,
        ),
        language="zh-CN",
    )


class FakeGithub:
    def __init__(
        self,
        *,
        issue: GitHubIssue,
        timeline: tuple[dict, ...] = (),
        known_commits: tuple[str, ...] = (),
        merged_prs: tuple[str, ...] = (),
        branch_fix_commits: dict[tuple[str, int], str] | None = None,
    ) -> None:
        self.issue = issue
        self.timeline = timeline
        self.comments: list[str] = []
        self.reopened = False
        self.known_commits = {sha.lower() for sha in known_commits}
        # entries are "owner/repo#number"
        self.merged_prs = {ref.lower() for ref in merged_prs}
        # (branch, issue_number) -> full SHA the branch scan should recover
        self.branch_fix_commits = branch_fix_commits or {}

    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        return self.issue

    def commit_exists(self, *, repo: str, sha: str) -> bool:
        return sha.lower() in self.known_commits

    def pull_request_merged(self, *, repo: str, number: int) -> bool:
        return f"{repo}#{number}".lower() in self.merged_prs

    def commit_referencing_issue(
        self, *, repo: str, branch: str, issue_number: int, max_commits: int = 200
    ) -> str:
        return self.branch_fix_commits.get((branch, issue_number), "")

    def reopen_issue(self, *, repo: str, issue_number: int) -> None:
        self.reopened = True

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

    def test_commit_sha_cited_in_comment_counts_as_evidence(self) -> None:
        # A fix committed directly to a feature branch has no GitHub-native link,
        # so a dev's "Fixed in <sha> ..." comment is honored when the SHA resolves.
        github = FakeGithub(issue=_closed_issue(), known_commits=("0223259",))
        github.comments.append("Fixed in 0223259 on 2026/chat-live: badge now reads 1 min.")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=self.config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.audited)
        self.assertEqual(summary.evidence, "commit 0223259 (cited in a comment)")
        self.assertFalse(summary.nagged)
        self.assertEqual(github.comments, ["Fixed in 0223259 on 2026/chat-live: badge now reads 1 min."])
        self.assertEqual(len(lark.replies), 0)

    def test_unresolvable_hex_in_comment_is_not_evidence(self) -> None:
        # A hex-shaped token that isn't a real commit (known_commits empty) does
        # not pass, so the missing-fix nag still fires.
        config = self._notify_config()
        github = FakeGithub(issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)))
        github.comments.append("looks done, ref deadbeef1234")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.nagged)
        self.assertEqual(summary.evidence, "")

    def test_cited_commit_short_circuits_reopen_enforcement(self) -> None:
        # The false-positive guard: an issue truly fixed on a feature branch and
        # documented with a real SHA must NOT be reopened under enforcement.
        config = self._enforce_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            known_commits=("0223259",),
        )
        github.comments.append("Fixed in 0223259 on 2026/chat-live.")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertFalse(summary.reopened)
        self.assertFalse(github.reopened)
        self.assertEqual(summary.evidence, "commit 0223259 (cited in a comment)")
        self.assertEqual(len(lark.replies), 0)

    def test_feature_branch_fix_commit_short_circuits_reopen_enforcement(self) -> None:
        # #4044 repro: a real fix commit referencing the issue is pushed to the
        # intake target branch. GitHub's `referenced` timeline event exists but is
        # invisible to the workflow's app token (non-default branch), so evidence
        # detection must fall back to scanning the target branch directly. It must
        # recover the commit and NOT reopen.
        config = self._enforce_config()
        github = FakeGithub(
            issue=_closed_issue(
                body=_managed_body(chat_id=config.lark.chat_id, target_branch="2026/chat-live")
            ),
            branch_fix_commits={("2026/chat-live", 7): "628b04f672034ccc"},
        )
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertFalse(summary.reopened)
        self.assertFalse(github.reopened)
        self.assertEqual(summary.evidence, "commit 628b04f672034ccc on 2026/chat-live")
        self.assertEqual(len(lark.replies), 0)

    def test_no_target_branch_fix_commit_still_reopens(self) -> None:
        # No fix commit on the target branch (and no other evidence) -> still
        # reopens, so the branch scan doesn't blanket-suppress enforcement.
        config = self._enforce_config()
        github = FakeGithub(
            issue=_closed_issue(
                body=_managed_body(chat_id=config.lark.chat_id, target_branch="2026/chat-live")
            ),
        )
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.reopened)
        self.assertTrue(github.reopened)
        self.assertEqual(summary.evidence, "")

    def _weaver_config(self):
        base = self._notify_config()
        return dataclasses.replace(
            base,
            reference_repos=(
                ReferenceRepo(repo="TheCloverLab/weaver", path="~/clover/weaver"),
            ),
        )

    def test_merged_cross_repo_pr_cited_in_comment_counts_as_evidence(self) -> None:
        # A weaver backend fix lands as a merged PR in the sibling repo, cited in
        # a comment ("PR: TheCloverLab/weaver#1000"). It must count as evidence.
        config = self._weaver_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            merged_prs=("TheCloverLab/weaver#1000",),
        )
        github.comments.append("这是 Server 端的修复，PR: TheCloverLab/weaver#1000")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="TheCloverLab/fived", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.audited)
        self.assertEqual(summary.evidence, "merged PR TheCloverLab/weaver#1000 (cited in a comment)")
        self.assertFalse(summary.nagged)
        self.assertEqual(github.comments, ["这是 Server 端的修复，PR: TheCloverLab/weaver#1000"])
        self.assertEqual(len(lark.replies), 0)

    def test_short_form_reference_repo_pr_counts(self) -> None:
        config = self._weaver_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            merged_prs=("TheCloverLab/weaver#1000",),
        )
        github.comments.append("fixed server-side in weaver#1000")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="TheCloverLab/fived", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertEqual(summary.evidence, "merged PR TheCloverLab/weaver#1000 (cited in a comment)")
        self.assertFalse(summary.nagged)

    def test_unmerged_cross_repo_pr_is_not_evidence(self) -> None:
        # weaver#1000 referenced but not merged (merged_prs empty) -> still nags.
        config = self._weaver_config()
        github = FakeGithub(issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)))
        github.comments.append("server fix in TheCloverLab/weaver#1000 (still in review)")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="TheCloverLab/fived", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.nagged)
        self.assertEqual(summary.evidence, "")

    def test_merged_pr_in_unlisted_repo_is_not_evidence(self) -> None:
        # A merged PR in a repo that is neither this repo nor a reference repo is
        # not honored, even if it happens to be merged.
        config = self._weaver_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            merged_prs=("SomeOther/repo#5",),
        )
        github.comments.append("see SomeOther/repo#5")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="TheCloverLab/fived", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.nagged)
        self.assertEqual(summary.evidence, "")

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

    def _enforce_config(self):
        base = self._notify_config()
        return dataclasses.replace(
            base,
            close_audit=dataclasses.replace(
                base.close_audit, reopen_completed_without_evidence=True
            ),
        )

    def test_reopens_completed_close_without_evidence(self) -> None:
        config = self._enforce_config()
        github = FakeGithub(issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)))
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.reopened)
        self.assertTrue(summary.notified)
        self.assertTrue(github.reopened)
        self.assertEqual(len(github.comments), 1)
        self.assertIn("已自动重新打开", github.comments[0])
        self.assertIn("Fixes #7", github.comments[0])
        self.assertEqual(len(lark.replies), 1)
        self.assertIn("已自动重新打开", lark.replies[0].text)
        self.assertIn('<at user_id="ou_dev">garlanddiego</at>', lark.replies[0].text)

    def test_reopen_re_fires_on_each_close_not_deduped_by_marker(self) -> None:
        # Dedup rides on issue state (the top guard), not the persistent marker,
        # so a genuine re-close (issue still closed, no evidence) reopens again.
        config = self._enforce_config()
        github = FakeGithub(issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)))
        lark = FakeLarkMessengerClient()

        first = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )
        second = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(first.reopened)
        self.assertTrue(second.reopened)
        self.assertEqual(len(github.comments), 2)
        self.assertEqual(len(lark.replies), 2)

    def test_reopen_dry_run_does_not_reopen(self) -> None:
        config = self._enforce_config()
        github = FakeGithub(issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)))

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, dry_run=True
        )

        self.assertTrue(summary.audited)
        self.assertFalse(summary.reopened)
        self.assertFalse(github.reopened)
        self.assertEqual(github.comments, [])

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
