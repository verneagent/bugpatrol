from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

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

    def _minimal_data(self, config, lark_extra: dict | None = None) -> dict:
        lark = {
            "chat_id": config.lark.chat_id,
            "app_id": config.lark.app_id,
            "app_secret_env": config.lark.app_secret_env,
            "bot_open_id": config.lark.bot_open_id,
        }
        lark.update(lark_extra or {})
        return {
            "github_repo": config.github_repo,
            "lark": lark,
            "triage_agent": {"runner_labels": list(config.triage_agent.runner_labels)},
            "prd": {"root_wiki_node": config.prd.root_wiki_node},
            "intake": {"language": config.intake.language},
            "issue_field_names": dict(config.issue_field_names),
        }

    def test_lark_platform_defaults_to_international(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        parsed = parse_project_config(self._minimal_data(config))

        self.assertEqual(parsed.lark.platform, "lark")
        self.assertEqual(parsed.lark.api_base_url, "https://open.larksuite.com/open-apis")
        self.assertEqual(
            parsed.lark.message_url_template,
            "https://applink.larksuite.com/client/chat/open?openChatId={chat_id}&messageId={message_id}",
        )

    def test_lark_platform_feishu_switches_api_and_link_domains(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        parsed = parse_project_config(self._minimal_data(config, {"platform": "feishu"}))

        self.assertEqual(parsed.lark.api_base_url, "https://open.feishu.cn/open-apis")
        self.assertEqual(
            parsed.lark.message_url_template,
            "https://applink.feishu.cn/client/chat/open?openChatId={chat_id}&messageId={message_id}",
        )

    def test_lark_platform_keeps_explicit_message_url_template(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        parsed = parse_project_config(
            self._minimal_data(
                config,
                {"platform": "feishu", "message_url_template": "https://custom.test/{chat_id}/{message_id}"},
            )
        )

        self.assertEqual(parsed.lark.message_url_template, "https://custom.test/{chat_id}/{message_id}")

    def test_lark_platform_rejects_unknown_value(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        with self.assertRaisesRegex(ValueError, "lark.platform"):
            parse_project_config(self._minimal_data(config, {"platform": "wechat"}))

    def test_branch_chats_defaults_to_none_and_main(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        parsed = parse_project_config(self._minimal_data(config))

        self.assertIsNone(parsed.lark.branch_chats)
        self.assertEqual(parsed.lark.branch_for_chat("oc_anything"), "main")
        self.assertEqual(parsed.lark.all_chat_ids(), (config.lark.chat_id,))

    def test_branch_chats_map_chat_to_branch(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        parsed = parse_project_config(
            self._minimal_data(
                config,
                {
                    "branch_chats": [
                        {"chat_id": "oc_feature", "branch": "feature/x"},
                        {"chat_id": "oc_other", "branch": "release/2"},
                    ]
                },
            )
        )

        self.assertEqual(parsed.lark.branch_for_chat("oc_feature"), "feature/x")
        self.assertEqual(parsed.lark.branch_for_chat("oc_other"), "release/2")
        self.assertEqual(parsed.lark.branch_for_chat(config.lark.chat_id), "main")
        self.assertEqual(
            parsed.lark.all_chat_ids(),
            (config.lark.chat_id, "oc_feature", "oc_other"),
        )

    def test_branch_chats_rejects_remapping_main_chat(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        with self.assertRaisesRegex(ValueError, "main lark.chat_id"):
            parse_project_config(
                self._minimal_data(
                    config,
                    {"branch_chats": [{"chat_id": config.lark.chat_id, "branch": "x"}]},
                )
            )

    def test_branch_chats_rejects_duplicate_chat(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_project_config(
                self._minimal_data(
                    config,
                    {
                        "branch_chats": [
                            {"chat_id": "oc_dup", "branch": "a"},
                            {"chat_id": "oc_dup", "branch": "b"},
                        ]
                    },
                )
            )

    def test_reference_repos_default_empty(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        parsed = parse_project_config(self._minimal_data(config))
        self.assertEqual(parsed.reference_repos, ())

    def test_reference_repos_parse_with_branch_map(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        data = self._minimal_data(config)
        data["triage"] = {
            "reference_repos": [
                {
                    "repo": "org/weaver",
                    "path": "weaver",
                    "purpose": "backend API",
                    "branch_map": {"2026/chat-live": "feature/live"},
                }
            ]
        }
        parsed = parse_project_config(data)
        self.assertEqual(len(parsed.reference_repos), 1)
        ref = parsed.reference_repos[0]
        self.assertEqual(ref.repo, "org/weaver")
        self.assertEqual(ref.path, "weaver")
        self.assertEqual(ref.purpose, "backend API")
        self.assertEqual(ref.branch_for("2026/chat-live"), "feature/live")
        self.assertEqual(ref.branch_for("main"), "main")

    def test_reference_repos_reject_duplicate_repo(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        data = self._minimal_data(config)
        data["triage"] = {
            "reference_repos": [
                {"repo": "org/weaver", "path": "weaver"},
                {"repo": "org/weaver", "path": "weaver2"},
            ]
        }
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_project_config(data)

    def test_reference_repos_reject_bad_branch_map(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        data = self._minimal_data(config)
        data["triage"] = {
            "reference_repos": [
                {"repo": "org/weaver", "path": "weaver", "branch_map": {"a": 1}},
            ]
        }
        with self.assertRaisesRegex(ValueError, "branch_map"):
            parse_project_config(data)

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

    def test_github_cli_env_override_beats_config(self) -> None:
        with mock.patch.dict(os.environ, {"BUGPATROL_GITHUB_CLI": "gh"}):
            parsed = parse_project_config(
                {
                    "github_repo": "owner/repo",
                    "github_cli": "~/clover/fived/scripts/gh-as-bot.sh",
                    "lark": {
                        "chat_id": "oc_x",
                        "app_id": "cli_x",
                        "app_secret_env": "SECRET_ENV",
                        "bot_open_id": "ou_x",
                    },
                    "triage_agent": {"runner_labels": ["self-hosted"]},
                    "prd": {"root_wiki_node": "NODE"},
                    "intake": {"language": "zh-CN"},
                    "issue_field_names": {},
                }
            )

        self.assertEqual(parsed.github_cli, "gh")

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
