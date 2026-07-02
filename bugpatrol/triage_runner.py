"""Prepare and optionally execute a triage agent run."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bugpatrol.agents import AgentInvocation, build_triage_agent_invocation
from bugpatrol.config import ProjectConfig
from bugpatrol.fields import TRIAGE_OUTPUT_SCHEMA
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.triage_context import build_triage_context, render_triage_context_markdown
from bugpatrol.triage_result import apply_triage_result, parse_triage_result


@dataclass(frozen=True)
class TriageRunPlan:
    context_path: Path
    schema_path: Path
    output_path: Path
    invocation: AgentInvocation


def prepare_triage_run(
    *,
    config: ProjectConfig,
    issue_number: int,
    repo_path: Path,
    output_dir: Path,
    github: GitHubCliIssuesClient,
    prompt_path: Path = Path("prompts/triage.zh.md"),
) -> TriageRunPlan:
    output_dir.mkdir(parents=True, exist_ok=True)
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    comments = github.list_issue_comments(repo=config.github_repo, issue_number=issue_number)
    context = build_triage_context(
        issue=issue,
        comments=comments,
        prd_root=repo_path / config.prd.cache_path,
        prd_include_globs=config.prd.include_globs,
    )
    context_path = output_dir / "triage-context.md"
    schema_path = output_dir / "triage.schema.json"
    output_path = output_dir / "triage-output.json"
    context_path.write_text(render_triage_context_markdown(context))
    schema_path.write_text(json.dumps(TRIAGE_OUTPUT_SCHEMA, ensure_ascii=False, indent=2))
    invocation = build_triage_agent_invocation(
        config,
        issue_number=issue_number,
        prompt_path=prompt_path,
        schema_path=schema_path,
        output_path=output_path,
        context_path=context_path,
    )
    return TriageRunPlan(
        context_path=context_path,
        schema_path=schema_path,
        output_path=output_path,
        invocation=invocation,
    )


def execute_triage_run(
    *,
    config: ProjectConfig,
    issue_number: int,
    plan: TriageRunPlan,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
) -> None:
    completed = subprocess.run(plan.invocation.command, check=False)
    if completed.returncode != 0:
        mark_triage_failed(
            config=config,
            issue_number=issue_number,
            exit_code=completed.returncode,
            github=github,
            issue_fields=issue_fields,
        )
        raise RuntimeError(f"triage agent failed with exit {completed.returncode}")
    result = parse_triage_result(json.loads(plan.output_path.read_text()))
    apply_triage_result(
        repo=config.github_repo,
        issue_number=issue_number,
        config=config,
        result=result,
        github=github,
        issue_fields=issue_fields,
    )


def mark_triage_failed(
    *,
    config: ProjectConfig,
    issue_number: int,
    exit_code: int,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
) -> None:
    issue_fields.add_issue_field_values(
        repo=config.github_repo,
        issue_number=issue_number,
        values={"Triage status": "Failed"},
        config=config,
    )
    github.add_issue_comment(
        repo=config.github_repo,
        issue_number=issue_number,
        body=render_triage_failed_comment(exit_code=exit_code),
    )


def render_triage_failed_comment(*, exit_code: int) -> str:
    return "\n".join(
        [
            "## BugPatrol triage failed",
            "",
            f"The triage agent exited with code `{exit_code}`.",
            "Check the runner logs, credentials, prompt/schema files, and repository checkout.",
        ]
    )
