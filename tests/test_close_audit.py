from __future__ import annotations

import dataclasses
import re
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
        "结论：预期行为。",
        {"version": 1, "issue": 7, "duplicate_of": 0, "verdict": "预期行为"},
    )


def _managed_body(chat_id: str = "oc_1", *, target_branch: str = "main") -> str:
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
        repo_commits: dict[str, tuple[str, ...]] | None = None,
        merged_prs: tuple[str, ...] = (),
        branch_commits: dict[str, tuple[tuple[str, str], ...]] | None = None,
    ) -> None:
        self.issue = issue
        self.timeline = timeline
        self.comments: list[str] = []
        self.reopened = False
        # SHAs that resolve in ANY repo (repo-agnostic).
        self.known_commits = {sha.lower() for sha in known_commits}
        # repo -> SHAs that resolve only in that repo (cross-repo evidence).
        self.repo_commits = {
            repo: {sha.lower() for sha in shas}
            for repo, shas in (repo_commits or {}).items()
        }
        # entries are "owner/repo#number"
        self.merged_prs = {ref.lower() for ref in merged_prs}
        # branch -> ((sha, message), ...) newest-first
        self.branch_commits = branch_commits or {}

    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        return self.issue

    def commit_exists(self, *, repo: str, sha: str) -> bool:
        sha = sha.lower()
        if sha in self.known_commits:
            return True
        return sha in self.repo_commits.get(repo, set())

    def pull_request_merged(self, *, repo: str, number: int) -> bool:
        return f"{repo}#{number}".lower() in self.merged_prs

    def commit_referencing_issue(
        self, *, repo: str, branch: str, issue_number: int, max_commits: int = 200
    ) -> str:
        pattern = re.compile(rf"#{issue_number}(?!\d)")
        for sha, message in self.branch_commits.get(branch, ())[:max_commits]:
            if pattern.search(message):
                return sha
        return ""

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

    def test_notifies_not_planned_even_after_expected_behavior_triage(self) -> None:
        # Triage no longer auto-closes 预期行为, so a not_planned close is always
        # an owner's own decision and must be announced.
        config = self._notify_config()
        github = FakeGithub(
            issue=_closed_issue(state_reason="not_planned", body=_managed_body(chat_id=config.lark.chat_id))
        )
        github.comments.append(render_expected_behavior_triage_comment())
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.notified)
        self.assertEqual(summary.kind, "closed_not_planned")
        self.assertEqual(len(lark.replies), 1)

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

    def test_commit_sha_cited_in_comment_counts_as_evidence_and_notifies(self) -> None:
        # A fix committed directly to a feature branch has no GitHub-native link,
        # so a dev's "Fixed in <sha> ..." comment is honored when the SHA resolves.
        # reconcile never announces such a bare feature-branch commit, so the close
        # is announced here (#4053 gap) -- not silently dropped.
        config = self._notify_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            known_commits=("0223259",),
        )
        github.comments.append("Fixed in 0223259 on 2026/chat-live: badge now reads 1 min.")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.audited)
        self.assertEqual(summary.kind, "closed_completed")
        self.assertEqual(summary.evidence, "commit 0223259 (cited in a comment)")
        self.assertTrue(summary.notified)
        self.assertFalse(summary.nagged)
        self.assertFalse(summary.reopened)
        self.assertEqual(len(lark.replies), 1)
        self.assertIn("已修复（completed）", lark.replies[0].text)
        # original cited comment + close-audit marker
        self.assertEqual(len(github.comments), 2)
        self.assertIn("已修复（completed）", github.comments[1])

    def test_cited_sha_survives_bot_metadata_hex_noise(self) -> None:
        # fived #4268: a long thread of bugpatrol metadata comments (triage run
        # UUIDs, Lark epoch-millis, numeric comment ids) all match the hex-run
        # shape. When they were collected as candidates they exhausted the
        # resolution cap before the dev's cited SHA, so a genuinely fixed issue
        # was reopened.
        config = self._notify_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            known_commits=("a431709f5",),
        )
        for index in range(6):
            github.comments.append(
                "<!-- BUGPATROL_TRIAGE_RUN_META\n"
                '{"context_comment_ids": ["508785341%d", "508792318%d"],'
                ' "run_id": "fc3b879a-e9a4-47b2-b8d0-683ff507ec0%d"}\n'
                "BUGPATROL_TRIAGE_RUN_META -->" % (index, index, index)
            )
            github.comments.append(
                f"## Lark 话题更新\n\n- 创建时间: 178513292240{index}\n\n## 消息\n\n看下这个"
            )
        github.comments.append("Fixed in a431709f5.")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertEqual(summary.evidence, "commit a431709f5 (cited in a comment)")
        self.assertFalse(summary.reopened)

    def test_completed_close_with_cited_fix_dedupes_on_rerun(self) -> None:
        config = self._notify_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            known_commits=("0223259",),
        )
        github.comments.append("Fixed in 0223259 on 2026/chat-live.")
        lark = FakeLarkMessengerClient()

        first = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )
        second = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(first.notified)
        self.assertFalse(second.notified)
        self.assertEqual(second.skipped_reason, "already notified")
        self.assertEqual(len(lark.replies), 1)

    def test_commit_url_cited_after_hex_noise_is_evidence(self) -> None:
        # #4070 regression: the real fix commit was linked via a /commit/<sha>
        # URL, but earlier comments were dense with hex-shaped noise (Lark
        # chat/message ids, triage run_id UUID fragments, numeric comment/user
        # ids). The bare-hex scanner used to exhaust its candidate cap on that
        # noise and never verify the genuinely-cited SHA, so enforcement kept
        # reopening a truly-fixed issue. The /commit/<sha> URL must win.
        sha = "60691f7dc943966a3f7456a04953113b02cd3edb"
        config = self._enforce_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            known_commits=(sha,),
        )
        # A pile of all-digit / UUID-fragment noise, then the real citation last.
        for noise in (
            "话题 oc_29b55b1560c31cafa9210fd4811cf3e0 消息 1784283944247",
            "run_id d4130bc1-3991-46c5-b6fd-5d2c7cc13ed9 comment 5002099665",
            "run_id 57f5ea01-5864-4ab6-b4b6-9782282ad4e0 user 2078038794371739648",
            "comment 5002144657 5002160762 5002277608 5002336712 5002463436",
        ):
            github.comments.append(noise)
        github.comments.append(
            f"Fixed by https://github.com/TheCloverLab/fived/commit/{sha}"
        )
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="TheCloverLab/fived", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertFalse(summary.reopened)
        self.assertFalse(github.reopened)
        self.assertEqual(summary.evidence, f"commit {sha} (cited in a comment)")
        self.assertTrue(summary.notified)

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
        # documented with a real SHA must NOT be reopened under enforcement -- but
        # the close is still announced to Lark.
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
        self.assertTrue(summary.notified)
        self.assertEqual(len(lark.replies), 1)

    def test_reverse_lookup_on_target_branch_short_circuits_reopen(self) -> None:
        # #4124/#4108: fixed by a commit made straight to the feature branch,
        # referencing the issue in its message -- but the App token can't see
        # that referenced timeline event and nobody pasted the SHA in a comment.
        # Reverse-look the intake-declared target branch: found -> notify, no
        # reopen (instead of the earlier wrong reopen).
        config = self._enforce_config()
        github = FakeGithub(
            issue=_closed_issue(
                body=_managed_body(chat_id=config.lark.chat_id, target_branch="feature-community")
            ),
            branch_commits={
                "feature-community": (
                    ("deadbeef1", "unrelated refactor"),
                    ("486265d0", "fix(header): route every nav-bar button (#7)"),
                ),
            },
        )
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertFalse(summary.reopened)
        self.assertFalse(github.reopened)
        self.assertEqual(summary.kind, "closed_completed")
        self.assertEqual(
            summary.evidence, "commit 486265d0 (references #7 on feature-community)"
        )
        self.assertTrue(summary.notified)
        self.assertEqual(len(lark.replies), 1)

    def test_reverse_lookup_without_reference_still_reopens(self) -> None:
        # Precision: a target branch with commits that do NOT reference the issue
        # is not evidence -- the completed close is still reopened under enforcement.
        config = self._enforce_config()
        github = FakeGithub(
            issue=_closed_issue(
                body=_managed_body(chat_id=config.lark.chat_id, target_branch="feature-community")
            ),
            branch_commits={
                "feature-community": (
                    ("deadbeef1", "fix something else (#70)"),
                    ("cafef00d2", "chore: bump deps"),
                ),
            },
        )
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.reopened)
        self.assertTrue(github.reopened)
        self.assertEqual(summary.kind, "missing_fix_reference")

    def test_reverse_lookup_skipped_for_main_target_branch(self) -> None:
        # A default-branch commit's referenced event IS visible to the App token,
        # so the timeline path already covers main -- reverse-lookup must skip it
        # (a bare #N in a main commit shouldn't override the timeline verdict).
        config = self._enforce_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            branch_commits={"main": (("abc12345", "fix: whatever (#7)"),)},
        )
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="o/r", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertTrue(summary.reopened)
        self.assertTrue(github.reopened)

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
        self.assertEqual(summary.kind, "closed_completed")
        self.assertEqual(summary.evidence, "merged PR TheCloverLab/weaver#1000 (cited in a comment)")
        self.assertFalse(summary.nagged)
        self.assertTrue(summary.notified)
        self.assertEqual(len(lark.replies), 1)

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

    def test_cross_repo_commit_url_cited_in_comment_counts_as_evidence(self) -> None:
        # A weaver backend fix cited as a /TheCloverLab/weaver/commit/<sha> URL.
        # It does not resolve in fived but does in the reference repo weaver, so
        # verifying against that URL's repo must count it as evidence.
        sha = "9f3c1ab7e2"
        config = self._weaver_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            repo_commits={"TheCloverLab/weaver": (sha,)},
        )
        github.comments.append(
            f"Server 端修复：https://github.com/TheCloverLab/weaver/commit/{sha}"
        )
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="TheCloverLab/fived", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertFalse(summary.nagged)
        self.assertEqual(summary.kind, "closed_completed")
        self.assertEqual(
            summary.evidence,
            f"commit {sha} in TheCloverLab/weaver (cited in a comment)",
        )
        self.assertTrue(summary.notified)

    def test_cross_repo_bare_sha_cited_in_comment_counts_as_evidence(self) -> None:
        # A weaver SHA pasted bare ("Fixed in <sha>"), no repo hint. It resolves
        # in the reference repo weaver (not fived), so trying each allowed repo
        # must find it.
        sha = "abc12345"
        config = self._weaver_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            repo_commits={"TheCloverLab/weaver": (sha,)},
        )
        github.comments.append(f"后端已修复，commit {sha}")
        lark = FakeLarkMessengerClient()

        summary = audit_issue_close(
            repo="TheCloverLab/fived", issue_number=7, config=config, github=github, lark=lark, dry_run=False
        )

        self.assertFalse(summary.nagged)
        self.assertEqual(
            summary.evidence,
            f"commit {sha} in TheCloverLab/weaver (cited in a comment)",
        )

    def test_commit_in_unlisted_repo_url_is_not_evidence(self) -> None:
        # A /commit/<sha> URL pointing at a repo that is neither this repo nor a
        # reference repo is ignored even if that SHA would resolve there.
        sha = "deadc0de1"
        config = self._weaver_config()
        github = FakeGithub(
            issue=_closed_issue(body=_managed_body(chat_id=config.lark.chat_id)),
            repo_commits={"SomeOther/repo": (sha,)},
        )
        github.comments.append(
            f"see https://github.com/SomeOther/repo/commit/{sha}"
        )
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
