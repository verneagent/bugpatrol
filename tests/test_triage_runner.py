from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
import subprocess
from pathlib import Path
from unittest.mock import patch

from bugpatrol.agents import AgentInvocation
from bugpatrol.clients import GitHubIssue, GitHubIssueComment
from bugpatrol.config import load_project_config
from bugpatrol.intake import IntakeRecord, render_issue_body
from bugpatrol.triage_runner import (
    TriageRunPlan,
    append_triage_run_metadata,
    comment_ids,
    execute_triage_run,
    list_known_assignees,
    list_matching_repo_branches,
    prepare_triage_run,
    render_triage_failed_comment,
)


class FakeGithub:
    def __init__(self, *, issue_body: str | None = None) -> None:
        self.comments: list[str] = ["Follow-up comment"]
        self.issue_types: list[str] = []
        self.assignees: list[str] = []
        self.issue_body = issue_body if issue_body is not None else managed_issue_body()

    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        return GitHubIssue(
            number=issue_number,
            url=f"https://github.test/{repo}/issues/{issue_number}",
            title="Todo empty state missing",
            body=self.issue_body,
        )

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        self.comments.append(body)

    def list_issue_comments(self, *, repo: str, issue_number: int) -> tuple[GitHubIssueComment, ...]:
        return tuple(
            GitHubIssueComment(id=str(index + 1), body=body)
            for index, body in enumerate(self.comments)
        )

    def set_issue_type(self, *, repo: str, issue_number: int, issue_type: str) -> None:
        self.issue_types.append(issue_type)

    def add_assignee(self, *, repo: str, issue_number: int, assignee: str) -> None:
        self.assignees.append(assignee)


class FakeIssueFields:
    def __init__(self) -> None:
        self.writes: list[dict[str, object]] = []

    def get_issue_field_values(self, **kwargs: object) -> dict[str, str]:
        return {}

    def add_issue_field_values(self, **kwargs: object) -> None:
        self.writes.append(kwargs)


class TriageRunnerTest(unittest.TestCase):
    def test_prepare_triage_run_writes_context_schema_and_command(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = replace(config, triage_agent=replace(config.triage_agent, provider="codex"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prd_root = root / config.prd.cache_path
            prd_root.mkdir(parents=True)
            (prd_root / "todo-list.md").write_text(
                "# Todo List PRD\n\nEmpty state appears after deleting all todos."
            )
            output_dir = root / "run"
            plan = prepare_triage_run(
                config=config,
                issue_number=7,
                repo_path=root,
                output_dir=output_dir,
                github=FakeGithub(),  # type: ignore[arg-type]
            )

            self.assertTrue(plan.context_path.exists())
            self.assertTrue(plan.schema_path.exists())
            self.assertEqual(plan.output_path, output_dir / "triage-output.json")
            self.assertIn("Todo empty state missing", plan.context_path.read_text())
            self.assertIn("Follow-up comment", plan.context_path.read_text())
            self.assertIn("triage-context.md", plan.invocation.command[-1])

    def test_prepare_triage_run_rejects_unmanaged_issue(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"

            with self.assertRaisesRegex(ValueError, "missing BUGPATROL_INTAKE_META"):
                prepare_triage_run(
                    config=config,
                    issue_number=7,
                    repo_path=root,
                    output_dir=output_dir,
                    github=FakeGithub(issue_body="legacy issue"),  # type: ignore[arg-type]
                )

            self.assertFalse(output_dir.exists())

    def test_comment_ids_ignore_triage_run_metadata_comments(self) -> None:
        comments = (
            GitHubIssueComment(id="1", body="Reporter follow-up"),
            GitHubIssueComment(id="2", body=append_triage_run_metadata({"version": 1, "run_id": "run-1"})),
        )

        self.assertEqual(comment_ids(comments), ("1",))

    def test_execute_triage_run_marks_failed_when_agent_exits_nonzero(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = TriageRunPlan(
                context_path=root / "context.md",
                schema_path=root / "schema.json",
                output_path=root / "output.json",
                invocation=AgentInvocation(provider="codex", command=["false"]),
            )

            with patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["false"], 42)
                with self.assertRaisesRegex(RuntimeError, "exit 42"):
                    execute_triage_run(
                        config=config,
                        issue_number=7,
                        plan=plan,
                        github=github,  # type: ignore[arg-type]
                        issue_fields=issue_fields,  # type: ignore[arg-type]
                    )

        self.assertEqual(issue_fields.writes[0]["values"], {"Triage status": "Running"})
        self.assertEqual(issue_fields.writes[1]["values"], {"Triage status": "Failed"})
        self.assertIn("exited with code `42`", github.comments[-1])

    def test_execute_triage_run_rejects_unmanaged_issue_before_writes(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub(issue_body="legacy issue")
        issue_fields = FakeIssueFields()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = TriageRunPlan(
                context_path=root / "context.md",
                schema_path=root / "schema.json",
                output_path=root / "output.json",
                invocation=AgentInvocation(provider="codex", command=["false"]),
            )

            with self.assertRaisesRegex(ValueError, "missing BUGPATROL_INTAKE_META"):
                execute_triage_run(
                    config=config,
                    issue_number=7,
                    plan=plan,
                    github=github,  # type: ignore[arg-type]
                    issue_fields=issue_fields,  # type: ignore[arg-type]
                )

        self.assertEqual(issue_fields.writes, [])
        self.assertEqual(github.comments, ["Follow-up comment"])

    def test_execute_triage_run_marks_needs_review_when_comments_changed(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output.json"
            output.write_text(
                """
                {
                  "issue_type": "Bug",
                  "priority": "High",
                  "triage_status": "Done",
                  "triage_verdict": "代码 Bug",
                  "platform": "Web",
                  "reproducibility": "必现",
                  "other_platforms": "未验证",
                  "capability": "Quest",
                  "evidence": "文字描述",
                  "prd_status": "已对齐",
                  "triage_confidence": "高",
                  "assignee": "octocat",
                  "owner_reason": "Manual",
                  "summary_cn": "空状态缺失",
                  "follow_up_questions": [],
                  "comment_markdown": "## Triage Analysis\\n\\nLooks like a code bug."
                }
                """
            )
            plan = TriageRunPlan(
                context_path=root / "context.md",
                schema_path=root / "schema.json",
                output_path=output,
                invocation=AgentInvocation(provider="codex", command=["true"]),
                context_comment_ids=("1",),
            )
            github.comments.append("New material after context generation")

            with patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["true"], 0)
                execute_triage_run(
                    config=config,
                    issue_number=7,
                    plan=plan,
                    github=github,  # type: ignore[arg-type]
                    issue_fields=issue_fields,  # type: ignore[arg-type]
                )

        self.assertEqual(issue_fields.writes[0]["values"], {"Triage status": "Running"})
        self.assertEqual(issue_fields.writes[1]["values"]["Triage status"], "Needs review")
        self.assertIn("new issue comments arrived", github.comments[-1])
        # CI runners hand the agent a never-closing stdin pipe; claude -p
        # blocks on it forever unless stdin is DEVNULL.
        self.assertEqual(run.call_args.kwargs.get("stdin"), subprocess.DEVNULL)

    def test_execute_triage_run_skips_apply_when_superseded(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output.json"
            output.write_text(valid_triage_output())
            plan = TriageRunPlan(
                context_path=root / "context.md",
                schema_path=root / "schema.json",
                output_path=output,
                invocation=AgentInvocation(provider="codex", command=["true"]),
                context_comment_ids=("1",),
            )

            def run_agent(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                github.comments.append(append_triage_run_metadata({"version": 1, "run_id": "run-new"}))
                return subprocess.CompletedProcess(["true"], 0)

            with patch("bugpatrol.triage_runner.uuid4", return_value="run-old"):
                with patch("subprocess.run", side_effect=run_agent):
                    execute_triage_run(
                        config=config,
                        issue_number=7,
                        plan=plan,
                        github=github,  # type: ignore[arg-type]
                        issue_fields=issue_fields,  # type: ignore[arg-type]
                    )

        self.assertEqual(issue_fields.writes[0]["values"], {"Triage status": "Running"})
        self.assertEqual(issue_fields.writes[1]["values"], {"Triage status": "Needs review"})
        self.assertEqual(github.issue_types, [])
        self.assertEqual(github.assignees, [])
        self.assertIn("superseded", github.comments[-1])

    def test_list_matching_repo_branches_filters_by_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), *env_args, "commit", "--allow-empty", "-q", "-m", "init"], check=True)
            subprocess.run(["git", "-C", str(root), "branch", "feature-login"], check=True)
            subprocess.run(["git", "-C", str(root), "branch", "release-9"], check=True)

            branches = list_matching_repo_branches(root, patterns=("main", "feature-*"))

        self.assertEqual(branches, ("feature-login", "main"))

    def test_list_matching_repo_branches_is_empty_for_non_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(list_matching_repo_branches(Path(tmp), patterns=("main",)), ())

    def test_execute_triage_run_rejects_fabricated_branch(self) -> None:
        config = load_project_config(Path("projects/full.example.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output.json"
            output.write_text(valid_triage_output(affected_branch="feature-ghost"))
            plan = TriageRunPlan(
                context_path=root / "context.md",
                schema_path=root / "schema.json",
                output_path=output,
                invocation=AgentInvocation(provider="codex", command=["true"]),
                context_comment_ids=("1",),
                known_branches=("main", "feature-login"),
            )

            with patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["true"], 0)
                execute_triage_run(
                    config=config,
                    issue_number=7,
                    plan=plan,
                    github=github,  # type: ignore[arg-type]
                    issue_fields=issue_fields,  # type: ignore[arg-type]
                )

        triage_comment = github.comments[-1]
        self.assertIn("feature-ghost", triage_comment)
        self.assertIn("未采信", triage_comment)
        for write in issue_fields.writes:
            self.assertNotIn("Affected branch", write["values"])

    def test_execute_triage_run_fails_when_agent_produces_no_output(self) -> None:
        config = load_project_config(Path("projects/full.example.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = TriageRunPlan(
                context_path=root / "context.md",
                schema_path=root / "schema.json",
                output_path=root / "output.json",
                invocation=AgentInvocation(provider="claude", command=["true"]),
                context_comment_ids=("1",),
            )

            with patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["true"], 0)
                with self.assertRaisesRegex(RuntimeError, "no output file"):
                    execute_triage_run(
                        config=config,
                        issue_number=7,
                        plan=plan,
                        github=github,  # type: ignore[arg-type]
                        issue_fields=issue_fields,  # type: ignore[arg-type]
                    )

        self.assertIn("BugPatrol triage failed", github.comments[-1])

    def test_render_triage_failed_comment_is_actionable(self) -> None:
        comment = render_triage_failed_comment(exit_code=2)

        self.assertIn("BugPatrol triage failed", comment)
        self.assertIn("credentials", comment)

    def test_list_known_assignees_merges_codeowners_and_config(self) -> None:
        config = load_project_config(Path("projects/full.example.toml"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github").mkdir()
            (root / ".github" / "CODEOWNERS").write_text(
                "# Andy (@AndyCokeZero) owns notifications\n"
                "/app/notifications.tsx @AndyCokeZero\n"
                "/app/* @garlanddiego @org/some-team\n"
            )

            assignees = list_known_assignees(root, config=config)

        self.assertIn("AndyCokeZero", assignees)
        self.assertIn("garlanddiego", assignees)
        # Team handles cannot be issue assignees.
        self.assertNotIn("org/some-team", assignees)
        # Display names from comments never leak in.
        self.assertNotIn("Andy", assignees)

    def test_execute_triage_run_fails_on_unknown_assignee(self) -> None:
        config = load_project_config(Path("projects/full.example.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output.json"
            output.write_text(valid_triage_output())  # assignee: octocat
            plan = TriageRunPlan(
                context_path=root / "context.md",
                schema_path=root / "schema.json",
                output_path=output,
                invocation=AgentInvocation(provider="codex", command=["true"]),
                context_comment_ids=("1",),
                known_assignees=("AndyCokeZero", "garlanddiego"),
            )

            with patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["true"], 0)
                with self.assertRaisesRegex(RuntimeError, "unknown assignee 'octocat'"):
                    execute_triage_run(
                        config=config,
                        issue_number=7,
                        plan=plan,
                        github=github,  # type: ignore[arg-type]
                        issue_fields=issue_fields,  # type: ignore[arg-type]
                    )

        self.assertEqual(github.assignees, [])
        self.assertIn("octocat", github.comments[-1])
        self.assertIn("AndyCokeZero", github.comments[-1])
        self.assertEqual(issue_fields.writes[-1]["values"], {"Triage status": "Failed"})


    def test_execute_triage_run_notifies_lark_on_running_and_failed(self) -> None:
        from bugpatrol.testing.fakes import FakeLarkMessengerClient

        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        lark = FakeLarkMessengerClient()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = TriageRunPlan(
                context_path=root / "context.md",
                schema_path=root / "schema.json",
                output_path=root / "output.json",
                invocation=AgentInvocation(provider="codex", command=["false"]),
            )

            with patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["false"], 42)
                with self.assertRaisesRegex(RuntimeError, "exit 42"):
                    execute_triage_run(
                        config=config,
                        issue_number=7,
                        plan=plan,
                        github=github,  # type: ignore[arg-type]
                        issue_fields=issue_fields,  # type: ignore[arg-type]
                        lark=lark,
                    )

        self.assertEqual(len(lark.replies), 2)
        self.assertIn("开始分诊", lark.replies[0].text)
        self.assertEqual(lark.replies[0].message_id, "om_1")
        self.assertIn("分诊失败", lark.replies[1].text)


def valid_triage_output(*, affected_branch: str = "") -> str:
    return f"""
    {{
      "affected_branch": "{affected_branch}",
      "issue_type": "Bug",
      "priority": "High",
      "triage_status": "Done",
      "triage_verdict": "代码 Bug",
      "platform": "Web",
      "reproducibility": "必现",
      "other_platforms": "未验证",
      "capability": "Quest",
      "evidence": "文字描述",
      "prd_status": "已对齐",
      "triage_confidence": "高",
      "assignee": "octocat",
      "owner_reason": "Manual",
      "summary_cn": "空状态缺失",
      "follow_up_questions": [],
      "comment_markdown": "## Triage Analysis\\n\\nLooks like a code bug."
    }}
    """


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


if __name__ == "__main__":
    unittest.main()
