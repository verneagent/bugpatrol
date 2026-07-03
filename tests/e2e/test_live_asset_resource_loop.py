from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import struct
import zlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from bugpatrol.backfill import intake_record_from_lark_message
from bugpatrol.config import load_project_config
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkOpenApiMessengerClient
from bugpatrol.resources import CommandResourceDescriber, GitHubAssetRepoStore, materialize_lark_attachments


def _png_64x64() -> bytes:
    width = 64
    height = 64
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend((220, 30, 40) if x < 48 and y < 48 else (250, 250, 250))
        rows.append(bytes(row))
    raw = b"".join(rows)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(raw)),
            _png_chunk(b"IEND", b""),
        ]
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_E2E") == "1", "live e2e is opt-in")
@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_ASSET_E2E") == "1", "asset repo write e2e is opt-in")
@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_ASSET_REPO"), "requires BUGPATROL_LIVE_ASSET_REPO")
class LiveAssetResourceLoopE2ETest(unittest.TestCase):
    def test_live_lark_image_reply_uploads_to_asset_repo_and_updates_issue(self) -> None:
        config = _load_live_config()
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
        describer = CommandResourceDescriber(
            command=config.media.description_command,
            timeout_seconds=config.media.description_timeout_seconds,
        )
        created_issue_number: int | None = None
        asset_url = ""

        try:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            seed = lark.send_chat_message(
                chat_id=config.lark.chat_id,
                text=f"BugPatrol live asset seed {stamp}.",
            )
            created = workflow.process(
                IntakeRecord(
                    reporter_name="BugPatrol Live E2E",
                    reporter_open_id=config.lark.bot_open_id,
                    created_at=datetime.now(UTC).isoformat(),
                    chat_id=config.lark.chat_id,
                    root_id=seed.message_id,
                    message_id=seed.message_id,
                    original_text=f"[test] live asset reply {stamp}",
                )
            )
            created_issue_number = created.issue.number

            image_key = lark.upload_image(filename="bugpatrol-live-asset.png", content=_png_64x64())
            sent = lark.reply_image_to_message(
                chat_id=config.lark.chat_id,
                message_id=seed.message_id,
                image_key=image_key,
            )
            message = lark.get_message(message_id=sent.message_id, default_chat_id=config.lark.chat_id)
            record = materialize_lark_attachments(
                record=intake_record_from_lark_message(message),
                lark=lark,
                store=store,
                describer=describer,
            )
            self.assertEqual(len(record.attachments), 1)
            asset_url = record.attachments[0].url
            self.assertIn(f"https://github.com/{config.assets.github_repo}/raw/{config.assets.branch}/", asset_url)

            outcome = workflow.process(record)
            self.assertEqual(outcome.action, "updated")
            comments = github.list_issue_comments(repo=config.github_repo, issue_number=created.issue.number)
            self.assertTrue(any(asset_url in comment.body for comment in comments))
            self.assertTrue(all("lark://message/" not in comment.body for comment in comments))
            self.assertTrue(any("生成描述" in comment.body for comment in comments))
            self.assertFalse(any("vision description unavailable" in comment.body for comment in comments))
            self.assertEqual(
                issue_fields.get_issue_field_values(
                    repo=config.github_repo,
                    issue_number=created.issue.number,
                )["Evidence"],
                "文字描述",
            )
            _assert_asset_exists_in_remote(asset_url, repo=config.assets.github_repo, branch=config.assets.branch)
        finally:
            if created_issue_number is not None:
                github.close_issue(repo=config.github_repo, issue_number=created_issue_number)
            if asset_url:
                _cleanup_asset(config=config, asset_url=asset_url)

    @unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_VIDEO_E2E") == "1", "video sender live e2e is opt-in")
    @unittest.skipUnless(shutil.which("ffmpeg"), "requires ffmpeg")
    @unittest.skipUnless(shutil.which("lark-cli"), "requires lark-cli")
    def test_live_lark_video_reply_uploads_to_asset_repo_and_updates_issue(self) -> None:
        config = _load_live_config()
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
        describer = CommandResourceDescriber(
            command=config.media.description_command,
            timeout_seconds=config.media.description_timeout_seconds,
        )
        created_issue_number: int | None = None
        asset_url = ""

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _write_test_video(tmp_path / "repro.mp4")
            (tmp_path / "cover.png").write_bytes(_png_64x64())
            try:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                seed = lark.send_chat_message(
                    chat_id=config.lark.chat_id,
                    text=f"BugPatrol live video asset seed {stamp}.",
                )
                created = workflow.process(
                    IntakeRecord(
                        reporter_name="BugPatrol Live Video E2E",
                        reporter_open_id=config.lark.bot_open_id,
                        created_at=datetime.now(UTC).isoformat(),
                        chat_id=config.lark.chat_id,
                        root_id=seed.message_id,
                        message_id=seed.message_id,
                        original_text=f"[test] live video asset reply {stamp}",
                    )
                )
                created_issue_number = created.issue.number

                with _temporary_lark_cli_config(app_id=config.lark.app_id, app_secret=app_secret):
                    completed = subprocess.run(
                        [
                            "lark-cli",
                            "im",
                            "+messages-reply",
                            "--message-id",
                            seed.message_id,
                            "--video",
                            "repro.mp4",
                            "--video-cover",
                            "cover.png",
                        ],
                        cwd=tmp_path,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

                message = _find_topic_media_message(
                    lark=lark,
                    chat_id=config.lark.chat_id,
                    root_id=seed.message_id,
                )
                record = materialize_lark_attachments(
                    record=intake_record_from_lark_message(message),
                    lark=lark,
                    store=store,
                    describer=describer,
                )
                self.assertEqual(record.attachments[0].kind, "video")
                asset_url = record.attachments[0].url
                self.assertTrue(asset_url.endswith(".mp4"))

                outcome = workflow.process(record)
                self.assertEqual(outcome.action, "updated")
                comments = github.list_issue_comments(repo=config.github_repo, issue_number=created.issue.number)
                self.assertTrue(any(asset_url in comment.body for comment in comments))
                self.assertTrue(any("生成描述" in comment.body for comment in comments))
                self.assertFalse(any("vision description unavailable" in comment.body for comment in comments))
                _assert_asset_exists_in_remote(asset_url, repo=config.assets.github_repo, branch=config.assets.branch)
            finally:
                if created_issue_number is not None:
                    github.close_issue(repo=config.github_repo, issue_number=created_issue_number)
                if asset_url:
                    _cleanup_asset(config=config, asset_url=asset_url)


def _load_live_config():
    config = load_project_config(Path("projects/todo-sandbox.toml"))
    asset_repo = os.environ.get("BUGPATROL_LIVE_ASSET_REPO", "").strip()
    if not asset_repo:
        return config
    config = replace(
        config,
        assets=replace(
            config.assets,
            github_repo=asset_repo,
            checkout_path=os.environ.get("BUGPATROL_LIVE_ASSET_CHECKOUT", config.assets.checkout_path),
            remote_url=os.environ.get("BUGPATROL_LIVE_ASSET_REMOTE_URL", f"https://github.com/{asset_repo}.git"),
        ),
    )
    return config


def _assert_asset_exists_in_remote(asset_url: str, *, repo: str, branch: str) -> None:
    rel_path = _asset_rel_path(asset_url, branch=branch)
    completed = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{repo}/contents/{rel_path}",
            "-f",
            f"ref={branch}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout.strip())


def _write_test_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
    )


def _find_topic_media_message(*, lark: LarkOpenApiMessengerClient, chat_id: str, root_id: str):
    for _ in range(6):
        messages = lark.list_chat_messages(chat_id=chat_id, limit=20)
        for message in messages:
            if message.root_id == root_id and message.msg_type == "media":
                return message
    raise AssertionError("media reply message not found")


@contextmanager
def _temporary_lark_cli_config(*, app_id: str, app_secret: str):
    config_path = Path("~/.lark-cli/config.json").expanduser()
    original = config_path.read_bytes()
    try:
        completed = subprocess.run(
            ["lark-cli", "config", "init", "--app-id", app_id, "--app-secret-stdin", "--brand", "lark"],
            input=app_secret,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr or completed.stdout)
        yield
    finally:
        config_path.write_bytes(original)


def _cleanup_asset(*, config, asset_url: str) -> None:  # type: ignore[no-untyped-def]
    rel_path = _asset_rel_path(asset_url, branch=config.assets.branch)
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


def _asset_rel_path(asset_url: str, *, branch: str) -> str:
    marker = f"/raw/{branch}/"
    if marker not in asset_url:
        raise AssertionError(f"unexpected asset URL: {asset_url}")
    return asset_url.split(marker, 1)[1]


if __name__ == "__main__":
    unittest.main()
