"""Command-line entry point for bugpatrol."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bugpatrol.agents import build_triage_agent_invocation
from bugpatrol.config import load_project_config
from bugpatrol.fields import TRIAGE_OUTPUT_SCHEMA, default_field_specs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bugpatrol")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-config", help="validate a project TOML config")
    validate.add_argument("path", type=Path)

    schema = sub.add_parser("schema", help="print the triage JSON schema")
    schema.add_argument("--pretty", action="store_true")

    agent = sub.add_parser("agent-command", help="print the configured triage agent command")
    agent.add_argument("project_config", type=Path)
    agent.add_argument("--issue", type=int, required=True)
    agent.add_argument("--prompt", type=Path, default=Path("prompts/triage.zh.md"))
    agent.add_argument("--schema", type=Path, default=Path("triage.schema.json"))
    agent.add_argument("--output", type=Path, default=Path("triage-output.json"))

    args = parser.parse_args(argv)

    if args.command == "validate-config":
        config = load_project_config(args.path)
        config.validate_against(default_field_specs())
        print(f"ok: {config.project}")
        return 0

    if args.command == "schema":
        if args.pretty:
            print(json.dumps(TRIAGE_OUTPUT_SCHEMA, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(TRIAGE_OUTPUT_SCHEMA, ensure_ascii=False, separators=(",", ":")))
        return 0

    if args.command == "agent-command":
        config = load_project_config(args.project_config)
        invocation = build_triage_agent_invocation(
            config,
            issue_number=args.issue,
            prompt_path=args.prompt,
            schema_path=args.schema,
            output_path=args.output,
        )
        print(json.dumps({"provider": invocation.provider, "command": invocation.command}, ensure_ascii=False))
        return 0

    parser.print_help(file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
