from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config, parse_project_config
from bugpatrol.fields import default_field_specs


class ConfigTest(unittest.TestCase):
    def test_fived_config_matches_canonical_fields(self) -> None:
        config = load_project_config(Path("projects/fived.toml"))

        self.assertEqual(config.project, "fived")
        self.assertEqual(config.github_repo, "TheCloverLab/fived")
        self.assertEqual(config.intake.language, "zh-CN")
        self.assertEqual(
            config.prd.include_globs,
            ("specs/**/spec.md", "changes/**/prd-snapshot.md"),
        )
        self.assertIn("fived-triage", config.triage_agent.runner_labels)
        config.validate_against(default_field_specs())

    def test_validation_rejects_missing_field(self) -> None:
        config = load_project_config(Path("projects/fived.toml"))
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
        config = load_project_config(Path("projects/fived.toml"))
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
        config = load_project_config(Path("projects/fived.toml"))
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
