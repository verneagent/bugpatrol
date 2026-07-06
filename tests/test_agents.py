from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.agents import build_triage_agent_invocation
from bugpatrol.config import load_project_config, parse_project_config


class AgentsTest(unittest.TestCase):
    def test_builds_subscription_friendly_codex_exec_command(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        invocation = build_triage_agent_invocation(
            config,
            issue_number=123,
            prompt_path=Path("prompts/triage.zh.md"),
            schema_path=Path("triage.schema.json"),
            output_path=Path("triage-output.json"),
            context_path=Path("triage-context.md"),
        )

        self.assertEqual(invocation.provider, "codex")
        self.assertEqual(invocation.command[:2], ["codex", "exec"])
        self.assertIn("--output-schema", invocation.command)
        self.assertNotIn("OPENAI_API_KEY", " ".join(invocation.command))
        self.assertIn("Issue: #123", invocation.command[-1])
        self.assertIn("triage-context.md", invocation.command[-1])

    def test_supports_claude_provider_boundary(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        claude_config = parse_project_config(
            {
                "github_repo": config.github_repo,
                "lark": {
                    "chat_id": config.lark.chat_id,
                    "app_id": config.lark.app_id,
                    "app_secret_env": config.lark.app_secret_env,
                    "bot_open_id": config.lark.bot_open_id,
                },
                "triage_agent": {
                    "provider": "claude",
                    "runner_labels": list(config.triage_agent.runner_labels),
                },
                "prd": {"root_wiki_node": config.prd.root_wiki_node},
                "intake": {"language": config.intake.language},
                "issue_field_names": dict(config.issue_field_names),
            }
        )

        invocation = build_triage_agent_invocation(
            claude_config,
            issue_number=123,
            prompt_path=Path("prompts/triage.zh.md"),
            schema_path=Path("triage.schema.json"),
            output_path=Path("triage-output.json"),
            context_path=None,
        )

        self.assertEqual(invocation.provider, "claude")
        self.assertEqual(invocation.command[0], "claude")
        # Non-interactive `claude -p` must not auto-deny the output file write.
        self.assertIn("--permission-mode", invocation.command)
        self.assertEqual(
            invocation.command[invocation.command.index("--permission-mode") + 1],
            "acceptEdits",
        )


if __name__ == "__main__":
    unittest.main()
