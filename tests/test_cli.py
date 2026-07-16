from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bugpatrol.__main__ import _parse_reference_repo_args, main
from bugpatrol.clients import GitHubIssue
from bugpatrol.config import load_project_config


class FakeGithub:
    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        return GitHubIssue(
            number=issue_number,
            url=f"https://github.test/{repo}/issues/{issue_number}",
            title="Legacy issue",
            body="created before BugPatrol",
        )

    def list_issue_comments(self, **kwargs: object) -> tuple[object, ...]:
        return ()


class ReferenceRepoArgsTest(unittest.TestCase):
    def test_parses_repo_equals_path_pairs(self) -> None:
        parsed = _parse_reference_repo_args(
            ["org/weaver=/cache/weaver", "org/aux=./aux"]
        )
        self.assertEqual(parsed["org/weaver"], Path("/cache/weaver"))
        self.assertEqual(parsed["org/aux"], Path("./aux"))

    def test_none_yields_empty_map(self) -> None:
        self.assertEqual(_parse_reference_repo_args(None), {})

    def test_rejects_missing_equals(self) -> None:
        with self.assertRaisesRegex(ValueError, "REPO=PATH"):
            _parse_reference_repo_args(["org/weaver"])

    def test_rejects_duplicate_repo(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _parse_reference_repo_args(["org/weaver=/a", "org/weaver=/b"])


class CliTest(unittest.TestCase):
    def test_notify_fix_skips_unmanaged_issue_without_failing(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        stdout = io.StringIO()

        with patch("bugpatrol.__main__.load_project_config", return_value=config):
            with patch("bugpatrol.__main__.GitHubCliIssuesClient", return_value=FakeGithub()):
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "notify-fix",
                            "projects/todo-sandbox.toml",
                            "--issue",
                            "7",
                            "--event",
                            "issue_fixed",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["skipped"])
        self.assertFalse(payload["lark_sent"])
        self.assertIn("no BugPatrol Lark intake metadata", payload["error"])

    def test_run_fix_execute_dispatches_to_run_fix(self) -> None:
        # Guards against the run-fix subparser variable shadowing the imported
        # run_fix function: with the shadow, this execute dispatch raised
        # "'ArgumentParser' object is not callable" instead of reaching run_fix.
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        stdout = io.StringIO()

        with patch("bugpatrol.__main__.load_project_config", return_value=config):
            with patch("bugpatrol.__main__.GitHubCliIssuesClient", return_value=FakeGithub()):
                with patch("bugpatrol.__main__.GitHubIssueFieldsClient", return_value=object()):
                    with patch("bugpatrol.__main__._optional_lark_client", return_value=None):
                        with patch("bugpatrol.__main__.run_fix", return_value="opened_pr") as run_fix:
                            with contextlib.redirect_stdout(stdout):
                                exit_code = main(
                                    [
                                        "run-fix",
                                        "projects/todo-sandbox.toml",
                                        "--issue",
                                        "7",
                                        "--repo-path",
                                        "/tmp/repo",
                                        "--execute",
                                    ]
                                )

        self.assertEqual(exit_code, 0)
        run_fix.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue())["status"], "opened_pr")

    def test_run_fix_revise_execute_dispatches_to_run_fix_revise(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        stdout = io.StringIO()

        with patch("bugpatrol.__main__.load_project_config", return_value=config):
            with patch("bugpatrol.__main__.GitHubCliIssuesClient", return_value=FakeGithub()):
                with patch("bugpatrol.__main__.GitHubIssueFieldsClient", return_value=object()):
                    with patch("bugpatrol.__main__._optional_lark_client", return_value=None):
                        with patch("bugpatrol.__main__.run_fix_revise", return_value="revised") as revise:
                            with contextlib.redirect_stdout(stdout):
                                exit_code = main(
                                    [
                                        "run-fix-revise",
                                        "projects/todo-sandbox.toml",
                                        "--issue",
                                        "7",
                                        "--repo-path",
                                        "/tmp/repo",
                                        "--execute",
                                    ]
                                )

        self.assertEqual(exit_code, 0)
        revise.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue())["status"], "revised")

    def test_run_ci_fix_execute_dispatches_to_run_ci_fix(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        stdout = io.StringIO()

        with patch("bugpatrol.__main__.load_project_config", return_value=config):
            with patch("bugpatrol.__main__.GitHubCliIssuesClient", return_value=FakeGithub()):
                with patch("bugpatrol.__main__.GitHubIssueFieldsClient", return_value=object()):
                    with patch("bugpatrol.__main__._optional_lark_client", return_value=None):
                        with patch("bugpatrol.__main__.run_ci_fix", return_value="ci_fixed") as ci_fix:
                            with contextlib.redirect_stdout(stdout):
                                exit_code = main(
                                    [
                                        "run-ci-fix",
                                        "projects/todo-sandbox.toml",
                                        "--issue",
                                        "7",
                                        "--head-sha",
                                        "deadbeef",
                                        "--repo-path",
                                        "/tmp/repo",
                                        "--execute",
                                    ]
                                )

        self.assertEqual(exit_code, 0)
        ci_fix.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue())["status"], "ci_fixed")

    def test_run_build_ready_execute_dispatches_to_run_build_ready(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        stdout = io.StringIO()

        with patch("bugpatrol.__main__.load_project_config", return_value=config):
            with patch("bugpatrol.__main__.GitHubCliIssuesClient", return_value=FakeGithub()):
                with patch("bugpatrol.__main__._optional_lark_client", return_value=None):
                    with patch(
                        "bugpatrol.__main__.run_build_ready", return_value="build_notified"
                    ) as build_ready:
                        with contextlib.redirect_stdout(stdout):
                            exit_code = main(
                                [
                                    "run-build-ready",
                                    "projects/todo-sandbox.toml",
                                    "--issue",
                                    "7",
                                    "--head-sha",
                                    "deadbeef",
                                    "--execute",
                                ]
                            )

        self.assertEqual(exit_code, 0)
        build_ready.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue())["status"], "build_notified")

    def test_run_triage_redirects_to_fix_revise_when_open_pr_exists(self) -> None:
        # A reporter follow-up re-triggers triage; when a fix PR is already open,
        # redirect to fix-revise (feed the correction in) instead of re-triaging
        # to a no-op "结论无变化".
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        stdout = io.StringIO()

        class RedirectGithub(FakeGithub):
            def get_open_pull_request_by_head(self, *, repo: str, head: str):
                from bugpatrol.clients import OpenPullRequest

                return OpenPullRequest(number=9, url="https://github.test/o/r/pull/9")

        dispatched: list[tuple] = []

        def fake_make_dispatch(cmd):
            def dispatch(issue_number: int) -> None:
                dispatched.append((tuple(cmd), issue_number))

            return dispatch

        with patch("bugpatrol.__main__.load_project_config", return_value=config):
            with patch("bugpatrol.__main__.GitHubCliIssuesClient", return_value=RedirectGithub()):
                with patch("bugpatrol.__main__.GitHubIssueFieldsClient", return_value=object()):
                    with patch("bugpatrol.__main__.make_dispatch", side_effect=fake_make_dispatch):
                        with patch("bugpatrol.__main__.resolve_issue_branch") as resolve:
                            with contextlib.redirect_stdout(stdout):
                                exit_code = main(
                                    [
                                        "run-triage",
                                        "projects/todo-sandbox.toml",
                                        "--issue",
                                        "7",
                                        "--repo-path",
                                        "/tmp/repo",
                                        "--execute",
                                        "--fix-revise-dispatch-command",
                                        "gh",
                                        "workflow",
                                        "run",
                                    ]
                                )

        self.assertEqual(exit_code, 0)
        # Triage was skipped entirely.
        resolve.assert_not_called()
        self.assertEqual(dispatched, [(("gh", "workflow", "run"), 7)])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["redirected"], "fix-revise")
        self.assertEqual(payload["pr"], 9)

    def test_run_triage_redirect_dispatch_handles_single_string_with_flags(self) -> None:
        # Locks the real production contract: the workflow passes the dispatch
        # command as ONE quoted string after --fix-revise-dispatch-command
        # (nargs="+"), and make_dispatch shlex-splits it. Passing the gh command
        # as multiple bare tokens would make argparse eat --repo/-f as options;
        # this test exercises the real make_dispatch (unpatched) + subprocess so a
        # regression in the single-string/{issue_number} contract fails here.
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        stdout = io.StringIO()

        class RedirectGithub(FakeGithub):
            def get_open_pull_request_by_head(self, *, repo: str, head: str):
                from bugpatrol.clients import OpenPullRequest

                return OpenPullRequest(number=9, url="https://github.test/o/r/pull/9")

        captured: list[list[str]] = []

        class _Completed:
            returncode = 0

        def fake_run(command, **kwargs):
            captured.append(list(command))
            return _Completed()

        with patch("bugpatrol.__main__.load_project_config", return_value=config):
            with patch("bugpatrol.__main__.GitHubCliIssuesClient", return_value=RedirectGithub()):
                with patch("bugpatrol.__main__.GitHubIssueFieldsClient", return_value=object()):
                    with patch("bugpatrol.slash_commands.subprocess.run", side_effect=fake_run):
                        with contextlib.redirect_stdout(stdout):
                            exit_code = main(
                                [
                                    "run-triage",
                                    "projects/todo-sandbox.toml",
                                    "--issue",
                                    "7",
                                    "--repo-path",
                                    "/tmp/repo",
                                    "--execute",
                                    "--fix-revise-dispatch-command",
                                    "gh workflow run bugpatrol-fix-revise.yml "
                                    "--repo TheCloverLab/fived -f "
                                    "issue_number={issue_number}",
                                ]
                            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            captured,
            [
                [
                    "gh",
                    "workflow",
                    "run",
                    "bugpatrol-fix-revise.yml",
                    "--repo",
                    "TheCloverLab/fived",
                    "-f",
                    "issue_number=7",
                ]
            ],
        )
        self.assertEqual(json.loads(stdout.getvalue())["redirected"], "fix-revise")

    def test_run_triage_no_redirect_when_no_open_pr(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))

        class NoPrGithub(FakeGithub):
            def get_open_pull_request_by_head(self, *, repo: str, head: str):
                return None

        dispatched: list[tuple] = []

        class _ReachedTriage(Exception):
            pass

        with patch("bugpatrol.__main__.load_project_config", return_value=config):
            with patch("bugpatrol.__main__.GitHubCliIssuesClient", return_value=NoPrGithub()):
                with patch("bugpatrol.__main__.GitHubIssueFieldsClient", return_value=object()):
                    with patch(
                        "bugpatrol.__main__.make_dispatch",
                        side_effect=lambda cmd: (lambda n: dispatched.append(n)),
                    ):
                        with patch(
                            "bugpatrol.__main__.resolve_issue_branch",
                            side_effect=_ReachedTriage,
                        ):
                            with self.assertRaises(_ReachedTriage):
                                main(
                                    [
                                        "run-triage",
                                        "projects/todo-sandbox.toml",
                                        "--issue",
                                        "7",
                                        "--repo-path",
                                        "/tmp/repo",
                                        "--execute",
                                        "--fix-revise-dispatch-command",
                                        "gh",
                                        "workflow",
                                        "run",
                                    ]
                                )

        # No open PR -> no redirect, normal triage path reached.
        self.assertEqual(dispatched, [])


if __name__ == "__main__":
    unittest.main()
