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
            config.prd.include_globs,
            ("**/*.md",),
        )
        self.assertEqual(config.assets.github_repo, "example-org/example-assets")
        self.assertEqual(config.assets.checkout_path, "~/example-assets")
        self.assertEqual(config.assets.base_path, ".github/issue-assets")
        self.assertEqual(config.assets.branch, "main")
        self.assertEqual(config.assets.remote_url, "https://github.com/example-org/example-assets.git")
        self.assertEqual(config.media.description_command[:3], ("python3", "-m", "bugpatrol.media_vision"))
        self.assertEqual(config.media.max_image_bytes, 10485760)
        self.assertEqual(config.media.max_video_bytes, 104857600)
        self.assertEqual(config.media.max_file_bytes, 52428800)
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
