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


if __name__ == "__main__":
    unittest.main()
