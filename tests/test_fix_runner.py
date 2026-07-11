from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bugpatrol.agents import AgentInvocation
from bugpatrol.clients import GitHubIssue, GitHubIssueComment
from bugpatrol.config import load_project_config
from bugpatrol.fix_runner import (
    FixRunPlan,
    execute_fix_run,
    latest_triage_analysis,
    prepare_fix_run,
    read_triage_verdict,
    render_fix_context_markdown,
    run_fix,
)
from bugpatrol.intake import IntakeRecord, render_issue_body
from bugpatrol.triage_result import append_triage_metadata


def managed_issue_body() -> str:
    return render_issue_body(
        IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id="oc_1",
            root_id="om_root",
            message_id="om_1",
            original_text="Deleting all todos does not show empty state.",
        )
    )


class FakeGithub:
    def __init__(self, *, comments=None, open_pr: str = "", assignees=()) -> None:
        self.comments = list(comments or [])
        self.added_comments: list[str] = []
        self.open_pr = open_pr
        self.created_prs: list[dict] = []
        self.reviewers: list[str] = []
        self.assignees = tuple(assignees)

    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        return GitHubIssue(
            number=issue_number,
            url=f"https://github.test/{repo}/issues/{issue_number}",
            title="Todo empty state missing",
            body=managed_issue_body(),
            assignees=self.assignees,
        )

    def list_issue_comments(self, *, repo: str, issue_number: int):
        return tuple(GitHubIssueComment(id=str(i + 1), body=b) for i, b in enumerate(self.comments))

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        self.added_comments.append(body)

    def find_open_pull_request_by_head(self, *, repo: str, head: str) -> str:
        return self.open_pr

    def create_pull_request(self, *, repo, head, base, title, body) -> str:
        self.created_prs.append({"head": head, "base": base, "title": title, "body": body})
        return "https://github.test/o/r/pull/9"

    def add_pull_request_reviewer(self, *, repo, pr, reviewer) -> None:
        self.reviewers.append(reviewer)


class FakeIssueFields:
    def __init__(self, verdict: str = "代码 Bug") -> None:
        self.verdict = verdict

    def get_issue_field_values(self, *, repo: str, issue_number: int) -> dict[str, str]:
        return {"Triage verdict": self.verdict}


def _sandbox_config():
    return load_project_config(Path("projects/todo-sandbox.toml"))


class PureHelpersTest(unittest.TestCase):
    def test_read_triage_verdict(self) -> None:
        config = _sandbox_config()
        self.assertEqual(
            read_triage_verdict(config=config, issue_number=7, issue_fields=FakeIssueFields("代码 Bug")),
            "代码 Bug",
        )

    def test_latest_triage_analysis_picks_marked_comment(self) -> None:
        plain = GitHubIssueComment(id="1", body="just chatter")
        triage = GitHubIssueComment(id="2", body=append_triage_metadata("## Triage\n根因分析", {"version": 1}))
        newer = GitHubIssueComment(id="3", body=append_triage_metadata("## Triage\n最新根因", {"version": 1}))
        self.assertIn("最新根因", latest_triage_analysis([plain, triage, newer]))

    def test_render_fix_context_markdown(self) -> None:
        issue = GitHubIssue(number=7, url="u", title="标题", body="正文")
        text = render_fix_context_markdown(
            issue=issue, verdict="代码 Bug", triage_analysis="根因是X", branch_note="目标分支：`feature-demo`"
        )
        self.assertIn("代码 Bug", text)
        self.assertIn("根因是X", text)
        self.assertIn("feature-demo", text)
        self.assertIn("正文", text)


class PrepareFixRunTest(unittest.TestCase):
    def test_writes_context_schema_and_absolute_paths(self) -> None:
        config = replace(_sandbox_config(), triage_agent=replace(_sandbox_config().triage_agent, provider="claude"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "wt"
            worktree.mkdir()
            output_dir = root / "out"
            plan = prepare_fix_run(
                config=config,
                issue_number=7,
                worktree_path=worktree,
                output_dir=output_dir,
                github=FakeGithub(comments=[append_triage_metadata("根因", {"version": 1})], assignees=("dev1",)),  # type: ignore[arg-type]
                issue_fields=FakeIssueFields(),  # type: ignore[arg-type]
                base_branch="feature-demo",
                head_branch="bugpatrol/fix-issue-7",
            )
            self.assertTrue(plan.context_path.exists())
            self.assertTrue(plan.schema_path.exists())
            self.assertEqual(plan.output_path, output_dir.resolve() / "fix-output.json")
            self.assertEqual(plan.agent_cwd, worktree.resolve())
            self.assertEqual(plan.verdict, "代码 Bug")
            self.assertEqual(plan.reviewer, "dev1")
            self.assertIn("根因", plan.context_path.read_text())


class RunFixGuardsTest(unittest.TestCase):
    def test_not_fixable_verdict_posts_blocked(self) -> None:
        config = _sandbox_config()
        github = FakeGithub()
        with tempfile.TemporaryDirectory() as tmp:
            status = run_fix(
                config=config,
                issue_number=7,
                base_repo=Path(tmp),
                output_dir=Path(tmp) / "out",
                github=github,  # type: ignore[arg-type]
                issue_fields=FakeIssueFields("信息不足"),  # type: ignore[arg-type]
            )
        self.assertEqual(status, "not_fixable")
        self.assertTrue(github.added_comments)
        self.assertIn("跳过", github.added_comments[0])

    def test_already_open_pr_short_circuits(self) -> None:
        config = _sandbox_config()
        github = FakeGithub(open_pr="https://github.test/o/r/pull/1")
        with tempfile.TemporaryDirectory() as tmp:
            status = run_fix(
                config=config,
                issue_number=7,
                base_repo=Path(tmp),
                output_dir=Path(tmp) / "out",
                github=github,  # type: ignore[arg-type]
                issue_fields=FakeIssueFields("代码 Bug"),  # type: ignore[arg-type]
            )
        self.assertEqual(status, "already_open_pr")
        self.assertFalse(github.created_prs)


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "src").mkdir()
    (path / "src" / "todo.ts").write_text("export const x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def _add_worktree(root: Path) -> Path:
    worktree = root / ".bugpatrol-worktrees" / "wt"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-b", "bugpatrol/fix-issue-7", str(worktree), "HEAD"],
        check=True,
        capture_output=True,
    )
    return worktree


class ExecuteFixRunTest(unittest.TestCase):
    def _plan(self, *, worktree: Path, output_dir: Path, verify: dict[str, str], command):
        # A real agent command (never a subprocess.run patch): globally patching
        # subprocess.run would also stub the real git calls this test needs.
        config = _sandbox_config()
        config = replace(config, fix=replace(config.fix, verify=verify))
        plan = FixRunPlan(
            context_path=output_dir / "fix-context.md",
            schema_path=output_dir / "fix.schema.json",
            output_path=output_dir / "fix-output.json",
            invocation=AgentInvocation(provider="deepseek", command=command, env={}, model="m"),
            agent_cwd=worktree,
            verdict="代码 Bug",
            base_branch="main",
            head_branch="bugpatrol/fix-issue-7",
            reviewer="dev1",
            reviewer_open_id="",
            branch_note="",
        )
        return config, plan

    @staticmethod
    def _edit_and_write_output(worktree: Path, output_path: Path) -> list[str]:
        payload = json.dumps(
            {
                "summary": "给空列表加空状态",
                "root_cause": "删除最后一条没渲染空态",
                "tests_added": True,
                "pr_title": "fix: 空状态",
                "pr_body": "修复空状态",
            }
        )
        script = (
            f"import json,pathlib;"
            f"pathlib.Path({str(worktree / 'src' / 'todo.ts')!r}).write_text('export const x = 2\\n');"
            f"pathlib.Path({str(output_path)!r}).write_text({payload!r})"
        )
        return ["python3", "-c", script]

    def test_blocked_when_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_git_repo(root)
            worktree = _add_worktree(root)
            output_dir = root / "out"
            output_dir.mkdir()
            # Agent that makes no edits at all.
            config, plan = self._plan(worktree=worktree, output_dir=output_dir, verify={"ok": "true"}, command=["true"])
            github = FakeGithub()
            status = execute_fix_run(
                config=config,
                issue=github.get_issue(repo=config.github_repo, issue_number=7),
                plan=plan,
                github=github,  # type: ignore[arg-type]
            )
            self.assertEqual(status, "no_changes")
            self.assertFalse(github.created_prs)

    def test_verify_failed_posts_and_opens_no_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_git_repo(root)
            worktree = _add_worktree(root)
            output_dir = root / "out"
            output_dir.mkdir()
            command = self._edit_and_write_output(worktree, output_dir / "fix-output.json")
            config, plan = self._plan(worktree=worktree, output_dir=output_dir, verify={"bad": "false"}, command=command)
            github = FakeGithub()
            status = execute_fix_run(
                config=config,
                issue=github.get_issue(repo=config.github_repo, issue_number=7),
                plan=plan,
                github=github,  # type: ignore[arg-type]
            )
            self.assertEqual(status, "verify_failed")
            self.assertFalse(github.created_prs)
            self.assertTrue(any("未通过验证" in c for c in github.added_comments))

    def test_opened_pr_on_clean_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_git_repo(root)
            worktree = _add_worktree(root)
            output_dir = root / "out"
            output_dir.mkdir()
            command = self._edit_and_write_output(worktree, output_dir / "fix-output.json")
            config, plan = self._plan(worktree=worktree, output_dir=output_dir, verify={"ok": "true"}, command=command)
            github = FakeGithub(assignees=("dev1",))
            with patch("bugpatrol.fix_runner.worktree_push_branch") as push:
                status = execute_fix_run(
                    config=config,
                    issue=github.get_issue(repo=config.github_repo, issue_number=7),
                    plan=plan,
                    github=github,  # type: ignore[arg-type]
                )
            self.assertEqual(status, "opened_pr")
            self.assertEqual(len(github.created_prs), 1)
            self.assertEqual(github.created_prs[0]["base"], "main")
            self.assertEqual(github.created_prs[0]["head"], "bugpatrol/fix-issue-7")
            self.assertIn("dev1", github.reviewers)
            push.assert_called_once()


if __name__ == "__main__":
    unittest.main()
