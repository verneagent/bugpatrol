from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config, parse_project_config
from bugpatrol.fields import default_field_specs


class ConfigTest(unittest.TestCase):
    def test_example_config_matches_canonical_fields(self) -> None:
        config = load_project_config(Path("projects/example.toml"))

        self.assertEqual(config.project, "example-app")
        self.assertEqual(config.github_repo, "example-org/example-app")
        self.assertEqual(config.intake.language, "zh-CN")
        self.assertEqual(
            config.lark.message_url_template,
            "https://applink.larksuite.com/client/chat/open?openChatId={chat_id}&messageId={message_id}",
        )
        self.assertEqual(
            config.lark.sender_names,
            {
                "cli_external_reporter": "External Reporter Bot",
                "ou_example_user": "Example QA",
            },
        )
        self.assertEqual(
            config.prd.include_globs,
            ("**/*.md",),
        )
        self.assertEqual(config.assets.github_repo, "example-org/example-assets")
        self.assertEqual(config.assets.checkout_path, "~/example-assets")
        self.assertEqual(config.assets.base_path, ".github/issue-assets")
        self.assertEqual(config.assets.branch, "main")
        self.assertEqual(config.assets.remote_url, "https://github.com/example-org/example-assets.git")
        self.assertEqual(config.media.description_command[:3], ("python3", "-m", "bugpatrol.media_vision"))
        self.assertEqual(config.media.description_retries, 2)
        self.assertEqual(config.media.description_retry_backoff_seconds, 1.0)
        self.assertEqual(config.media.redaction_command, ())
        self.assertEqual(config.media.redaction_timeout_seconds, 300)
        self.assertEqual(config.media.resize_max_image_width, 1600)
        self.assertEqual(config.media.resize_max_image_height, 1600)
        self.assertEqual(config.media.resize_image_quality, 85)
        self.assertEqual(config.media.max_image_bytes, 10485760)
        self.assertEqual(config.media.max_video_bytes, 104857600)
        self.assertEqual(config.media.max_file_bytes, 52428800)
        self.assertEqual(config.media.max_video_duration_seconds, 120)
        self.assertEqual(config.media.video_probe_command, ())
        self.assertEqual(config.media.video_probe_timeout_seconds, 30)
        self.assertEqual(config.media.video_frame_command, ())
        self.assertEqual(config.media.video_frame_timeout_seconds, 300)
        self.assertEqual(config.media.video_frame_min_duration_seconds, 0)
        self.assertEqual(config.owners.default, ("@example-triage",))
        self.assertEqual(config.owners.paths, {"src/auth/**": ("@example-auth-owner",)})
        self.assertEqual(config.owners.capabilities, {"Quest": ("@example-quest-owner",)})
        self.assertEqual(config.followup_classifier.acknowledgement_texts, ("已收到",))
        self.assertEqual(config.followup_classifier.fix_status_keywords, ("已发版",))
        self.assertIn("bugpatrol-example-triage", config.triage_agent.runner_labels)
        config.validate_against(default_field_specs())

    def test_sandbox_has_configured_asset_repo(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))

        self.assertEqual(config.assets.github_repo, "TheCloverLab/bugpatrol-todo-sandbox-assets")
        self.assertEqual(config.assets.checkout_path, "~/clover/bugpatrol-todo-sandbox-assets")
        self.assertEqual(config.assets.remote_url, "https://github.com/TheCloverLab/bugpatrol-todo-sandbox-assets.git")
        self.assertEqual(config.media.description_command[:3], ("python3", "-m", "bugpatrol.media_vision"))

    def test_minimal_and_full_examples_validate(self) -> None:
        for path in (Path("projects/minimal.example.toml"), Path("projects/full.example.toml")):
            with self.subTest(path=path):
                config = load_project_config(path)
                config.validate_against(default_field_specs())

    def test_branches_default_to_main_when_section_missing(self) -> None:
        config = load_project_config(Path("projects/example.toml"))

        self.assertEqual(config.branches.default, "main")
        self.assertEqual(config.branches.allowed, ("main",))

    def test_full_example_parses_branch_patterns(self) -> None:
        config = load_project_config(Path("projects/full.example.toml"))

        self.assertEqual(config.branches.default, "main")
        self.assertEqual(config.branches.allowed, ("main", "post", "chat-live", "feature-*"))

    def test_branches_default_must_match_allowed_patterns(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        with self.assertRaisesRegex(ValueError, "branches.default"):
            parse_project_config(
                {
                    "github_repo": config.github_repo,
                    "lark": {
                        "chat_id": config.lark.chat_id,
                        "app_id": config.lark.app_id,
                        "app_secret_env": config.lark.app_secret_env,
                        "bot_open_id": config.lark.bot_open_id,
                    },
                    "triage_agent": {"runner_labels": list(config.triage_agent.runner_labels)},
                    "prd": {"root_wiki_node": config.prd.root_wiki_node},
                    "intake": {"language": config.intake.language},
                    "branches": {"default": "develop", "allowed": ["main", "feature-*"]},
                    "issue_field_names": dict(config.issue_field_names),
                }
            )

    def test_validation_rejects_missing_field(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        broken = parse_project_config(
            {
                "github_repo": config.github_repo,
                "lark": {
                    "chat_id": config.lark.chat_id,
                    "app_id": config.lark.app_id,
                    "app_secret_env": config.lark.app_secret_env,
                    "bot_open_id": config.lark.bot_open_id,
                },
                "triage_agent": {"runner_labels": list(config.triage_agent.runner_labels)},
                "prd": {"root_wiki_node": config.prd.root_wiki_node},
                "intake": {"language": config.intake.language},
                "issue_field_names": {
                    key: value
                    for key, value in config.issue_field_names.items()
                    if key != "Triage status"
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "Triage status"):
            broken.validate_against(default_field_specs())

    def test_intake_since_and_orphan_flag_parse(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        parsed = parse_project_config(
            {
                "github_repo": config.github_repo,
                "lark": {
                    "chat_id": config.lark.chat_id,
                    "app_id": config.lark.app_id,
                    "app_secret_env": config.lark.app_secret_env,
                    "bot_open_id": config.lark.bot_open_id,
                },
                "triage_agent": {"runner_labels": list(config.triage_agent.runner_labels)},
                "prd": {"root_wiki_node": config.prd.root_wiki_node},
                "intake": {
                    "language": config.intake.language,
                    "since": "2026-07-06T00:00:00+08:00",
                    "skip_orphan_replies": True,
                },
                "issue_field_names": dict(config.issue_field_names),
            }
        )

        self.assertEqual(parsed.intake.since, "2026-07-06T00:00:00+08:00")
        self.assertTrue(parsed.intake.skip_orphan_replies)
        self.assertEqual(parsed.intake.since_ms(), 1783267200000)

    def test_github_cli_expands_user_home(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        parsed = parse_project_config(
            {
                "github_repo": config.github_repo,
                "github_cli": "~/clover/fived/scripts/gh-as-bot.sh",
                "lark": {
                    "chat_id": config.lark.chat_id,
                    "app_id": config.lark.app_id,
                    "app_secret_env": config.lark.app_secret_env,
                    "bot_open_id": config.lark.bot_open_id,
                },
                "triage_agent": {"runner_labels": list(config.triage_agent.runner_labels)},
                "prd": {"root_wiki_node": config.prd.root_wiki_node},
                "intake": {"language": config.intake.language},
                "issue_field_names": dict(config.issue_field_names),
            }
        )

        self.assertEqual(
            parsed.github_cli,
            str(Path.home() / "clover/fived/scripts/gh-as-bot.sh"),
        )

    def test_github_cli_defaults_to_plain_gh(self) -> None:
        config = load_project_config(Path("projects/example.toml"))

        self.assertEqual(config.github_cli, "gh")

    def test_intake_defaults_have_no_since_cutoff(self) -> None:
        config = load_project_config(Path("projects/example.toml"))

        self.assertEqual(config.intake.since, "")
        self.assertFalse(config.intake.skip_orphan_replies)
        self.assertEqual(config.intake.since_ms(), 0)

    def test_config_rejects_invalid_intake_since(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        with self.assertRaises(ValueError):
            parse_project_config(
                {
                    "github_repo": config.github_repo,
                    "lark": {
                        "chat_id": config.lark.chat_id,
                        "app_id": config.lark.app_id,
                        "app_secret_env": config.lark.app_secret_env,
                        "bot_open_id": config.lark.bot_open_id,
                    },
                    "triage_agent": {"runner_labels": list(config.triage_agent.runner_labels)},
                    "prd": {"root_wiki_node": config.prd.root_wiki_node},
                    "intake": {"language": config.intake.language, "since": "yesterday"},
                    "issue_field_names": dict(config.issue_field_names),
                }
            )

    def test_config_rejects_unknown_intake_language(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        with self.assertRaisesRegex(ValueError, "intake.language"):
            parse_project_config(
                {
                    "github_repo": config.github_repo,
                    "lark": {
                        "chat_id": config.lark.chat_id,
                        "app_id": config.lark.app_id,
                        "app_secret_env": config.lark.app_secret_env,
                        "bot_open_id": config.lark.bot_open_id,
                    },
                    "triage_agent": {"runner_labels": list(config.triage_agent.runner_labels)},
                    "prd": {"root_wiki_node": config.prd.root_wiki_node},
                    "intake": {"language": "fr-FR"},
                    "issue_field_names": dict(config.issue_field_names),
                }
            )

    def test_validation_rejects_live_github_option_drift(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        live_options = {
            github_name: spec.values
            for logical_name, spec in default_field_specs().items()
            for github_name in [config.issue_field_names[logical_name]]
        }
        live_options["Platform"] = ("iOS", "Android")

        with self.assertRaisesRegex(ValueError, "Platform"):
            config.validate_github_field_options(live_options, default_field_specs())


if __name__ == "__main__":
    unittest.main()
