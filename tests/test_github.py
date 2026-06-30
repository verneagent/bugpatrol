from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from bugpatrol.github import GitHubCliError, GitHubCliIssuesClient


class GitHubCliIssuesClientTest(unittest.TestCase):
    def test_find_issue_by_intake_root_reads_issue_bodies(self) -> None:
        client = GitHubCliIssuesClient()
        stdout = json.dumps(
            [
                {
                    "number": 7,
                    "url": "https://github.com/o/r/issues/7",
                    "title": "bug",
                    "body": '<!-- BUGPATROL_INTAKE_META:{"chat_id":"oc_1","root_id":"om_1"} -->',
                }
            ]
        )

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, stdout, "")
            issue = client.find_issue_by_intake_root(repo="o/r", chat_id="oc_1", root_id="om_1")

        self.assertIsNotNone(issue)
        self.assertEqual(issue.number, 7)
        run.assert_called_once()
        self.assertIn("--state", run.call_args.args[0])
        self.assertIn("all", run.call_args.args[0])

    def test_create_issue_posts_body_via_stdin_and_parses_url(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["gh"], 0, "https://github.com/o/r/issues/42\n", ""
            )
            issue = client.create_issue(
                repo="o/r",
                title="title",
                body="body",
                issue_type="Bug",
                fields={"Source": "Lark"},
            )

        self.assertEqual(issue.number, 42)
        self.assertEqual(issue.url, "https://github.com/o/r/issues/42")
        self.assertEqual(run.call_args.kwargs["input"], "body")
        self.assertIn("--body-file", run.call_args.args[0])

    def test_add_issue_comment_uses_body_file_stdin(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, "", "")
            client.add_issue_comment(repo="o/r", issue_number=42, body="comment")

        self.assertEqual(run.call_args.kwargs["input"], "comment")
        self.assertIn("comment", run.call_args.args[0])

    def test_raises_on_gh_failure(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 1, "", "bad auth")
            with self.assertRaisesRegex(GitHubCliError, "bad auth"):
                client.find_issue_by_intake_root(repo="o/r", chat_id="oc", root_id="om")


if __name__ == "__main__":
    unittest.main()
