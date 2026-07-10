from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from bugpatrol.agents import (
    DEEPSEEK_ANTHROPIC_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    build_triage_agent_invocation,
    parse_claude_token_usage,
)
from bugpatrol.config import load_project_config, parse_project_config


def _config_with_provider(provider: str, *, model: str = ""):
    config = load_project_config(Path("projects/example.toml"))
    return parse_project_config(
        {
            "github_repo": config.github_repo,
            "lark": {
                "chat_id": config.lark.chat_id,
                "app_id": config.lark.app_id,
                "app_secret_env": config.lark.app_secret_env,
                "bot_open_id": config.lark.bot_open_id,
            },
            "triage_agent": {
                "provider": provider,
                "model": model,
                "runner_labels": list(config.triage_agent.runner_labels),
            },
            "prd": {"root_wiki_node": config.prd.root_wiki_node},
            "intake": {"language": config.intake.language},
            "issue_field_names": dict(config.issue_field_names),
        }
    )


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
            workspace_dirs=(Path("/runner/bugpatrol/prompts"), Path("/runner/out")),
        )

        self.assertEqual(invocation.provider, "claude")
        self.assertEqual(invocation.command[0], "claude")
        # Non-interactive `claude -p` cannot answer approval prompts, so the
        # runner bypasses them wholesale (Bash dedup/git + output write).
        self.assertIn("--dangerously-skip-permissions", invocation.command)
        self.assertNotIn("--permission-mode", invocation.command)
        # Each workspace dir outside the checkout is re-admitted via --add-dir.
        self.assertEqual(
            [
                invocation.command[i + 1]
                for i, token in enumerate(invocation.command)
                if token == "--add-dir"
            ],
            ["/runner/bugpatrol/prompts", "/runner/out"],
        )


    def test_deepseek_provider_uses_claude_cli_with_endpoint_env(self) -> None:
        config = _config_with_provider("deepseek")

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            invocation = build_triage_agent_invocation(
                config,
                issue_number=7,
                prompt_path=Path("prompts/triage.zh.md"),
                schema_path=Path("triage.schema.json"),
                output_path=Path("triage-output.json"),
                context_path=None,
            )

        self.assertEqual(invocation.provider, "deepseek")
        self.assertEqual(invocation.command[0], "claude")
        self.assertIn("--dangerously-skip-permissions", invocation.command)
        self.assertEqual(
            invocation.command[invocation.command.index("--model") + 1],
            DEEPSEEK_DEFAULT_MODEL,
        )
        self.assertEqual(invocation.env["ANTHROPIC_BASE_URL"], DEEPSEEK_ANTHROPIC_BASE_URL)
        self.assertEqual(invocation.env["ANTHROPIC_API_KEY"], "sk-test")
        self.assertEqual(invocation.env["ANTHROPIC_AUTH_TOKEN"], "sk-test")
        # The key must never leak into argv (visible in ps / run logs).
        self.assertNotIn("sk-test", " ".join(invocation.command))

    def test_deepseek_provider_honors_configured_model(self) -> None:
        config = _config_with_provider("deepseek", model="deepseek-v4-flash")

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
            invocation = build_triage_agent_invocation(
                config,
                issue_number=7,
                prompt_path=Path("prompts/triage.zh.md"),
                schema_path=Path("triage.schema.json"),
                output_path=Path("triage-output.json"),
                context_path=None,
            )

        self.assertEqual(
            invocation.command[invocation.command.index("--model") + 1],
            "deepseek-v4-flash",
        )

    def test_deepseek_provider_requires_api_key(self) -> None:
        config = _config_with_provider("deepseek")

        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                build_triage_agent_invocation(
                    config,
                    issue_number=7,
                    prompt_path=Path("prompts/triage.zh.md"),
                    schema_path=Path("triage.schema.json"),
                    output_path=Path("triage-output.json"),
                    context_path=None,
                )

    def test_non_deepseek_providers_have_empty_env(self) -> None:
        config = load_project_config(Path("projects/example.toml"))
        invocation = build_triage_agent_invocation(
            config,
            issue_number=1,
            prompt_path=Path("prompts/triage.zh.md"),
            schema_path=Path("triage.schema.json"),
            output_path=Path("triage-output.json"),
            context_path=None,
        )

        self.assertEqual(invocation.env, {})


class ParseClaudeTokenUsageTest(unittest.TestCase):
    def test_splits_real_input_from_cache(self) -> None:
        # Shape emitted by the deepseek `result` event in stream-json output.
        stdout = json.dumps(
            {
                "type": "result",
                "usage": {
                    "input_tokens": 36705,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 604160,
                    "output_tokens": 11525,
                },
            }
        )
        self.assertEqual(parse_claude_token_usage(stdout), (36705, 604160, 11525))

    def test_returns_zeros_when_unparseable(self) -> None:
        self.assertEqual(parse_claude_token_usage(""), (0, 0, 0))
        self.assertEqual(parse_claude_token_usage("not json"), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
