from __future__ import annotations

import base64
import os
import subprocess
import unittest
from pathlib import Path

from bugpatrol.backfill import intake_record_from_lark_message
from bugpatrol.config import load_project_config
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkOpenApiMessengerClient
from bugpatrol.resources import GitHubAssetRepoStore, materialize_lark_attachments


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_E2E") == "1", "live e2e is opt-in")
@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_ASSET_E2E") == "1", "asset repo write e2e is opt-in")
class LiveAssetResourceLoopE2ETest(unittest.TestCase):
    def test_live_lark_image_resource_uploads_to_asset_repo_and_creates_issue(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        app_secret = os.environ[config.lark.app_secret_env]
        lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        issue_fields = GitHubIssueFieldsClient()
        github = GitHubCliIssuesClient(issue_fields=issue_fields, project_config=config)
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        store = GitHubAssetRepoStore(
            repo=config.assets.github_repo,
            checkout_path=Path(config.assets.checkout_path),
            base_path=config.assets.base_path,
            branch=config.assets.branch,
            remote_url=config.assets.remote_url,
        )
        created_issue_number: int | None = None
        asset_url = ""

        try:
            image_key = lark.upload_image(filename="bugpatrol-live-asset.png", content=PNG_1X1)
            sent = lark.send_chat_image(chat_id=config.lark.chat_id, image_key=image_key)
            message = lark.get_message(message_id=sent.message_id, default_chat_id=config.lark.chat_id)
            record = materialize_lark_attachments(
                record=intake_record_from_lark_message(message),
                lark=lark,
                store=store,
            )
            self.assertEqual(len(record.attachments), 1)
            asset_url = record.attachments[0].url
            self.assertIn("https://github.com/TheCloverLab/fived-assets/raw/main/", asset_url)

            outcome = workflow.process(record)
            created_issue_number = outcome.issue.number
            issue = github.get_issue(repo=config.github_repo, issue_number=outcome.issue.number)
            self.assertIn(asset_url, issue.body)
            self.assertNotIn("lark://message/", issue.body)
            self.assertEqual(
                issue_fields.get_issue_field_values(
                    repo=config.github_repo,
                    issue_number=outcome.issue.number,
                )["Evidence"],
                "截图",
            )
            _assert_asset_exists_in_remote(asset_url)
        finally:
            if created_issue_number is not None:
                github.close_issue(repo=config.github_repo, issue_number=created_issue_number)
            if asset_url:
                _cleanup_asset(config=config, asset_url=asset_url)


def _assert_asset_exists_in_remote(asset_url: str) -> None:
    rel_path = _asset_rel_path(asset_url)
    completed = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/TheCloverLab/fived-assets/contents/{rel_path}",
            "-f",
            "ref=main",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout.strip())


def _cleanup_asset(*, config, asset_url: str) -> None:  # type: ignore[no-untyped-def]
    rel_path = _asset_rel_path(asset_url)
    checkout = Path(config.assets.checkout_path).expanduser()
    subprocess.run(["git", "-C", str(checkout), "rm", "-f", rel_path], check=False)
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "commit",
            "--no-verify",
            "-m",
            f"test: remove bug attachment {Path(rel_path).parent.name}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode == 0:
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "push",
                "--no-verify",
                config.assets.remote_url or "origin",
                config.assets.branch,
            ],
            check=False,
        )


def _asset_rel_path(asset_url: str) -> str:
    marker = "/raw/main/"
    if marker not in asset_url:
        raise AssertionError(f"unexpected asset URL: {asset_url}")
    return asset_url.split(marker, 1)[1]


if __name__ == "__main__":
    unittest.main()
