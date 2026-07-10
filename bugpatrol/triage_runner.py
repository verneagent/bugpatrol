"""Prepare and optionally execute a triage agent run."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from bugpatrol.agents import AgentInvocation, build_triage_agent_invocation, parse_claude_token_usage
from bugpatrol.clients import GitHubIssueComment, LarkMessengerClient
from bugpatrol.config import ProjectConfig
from bugpatrol.fields import triage_output_schema
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import (
    branch_tip_sha_from_metadata,
    require_bugpatrol_managed_issue,
    target_branch_from_metadata,
)
from bugpatrol.ownership import load_codeowners, load_codeowners_identities
from bugpatrol.worktree import (
    BranchResolution,
    GitDriver,
    SubprocessGitDriver,
    resolve_triage_branch,
)
from bugpatrol.triage_context import (
    AssigneeIdentity,
    build_triage_context,
    render_triage_context_markdown,
)
from bugpatrol.triage_result import (
    TriageResult,
    TriageRunStats,
    apply_triage_result,
    parse_triage_result,
    send_intake_topic_message,
    triage_runner_name,
)


TRIAGE_RUN_META_START = "<!-- BUGPATROL_TRIAGE_RUN_META"
TRIAGE_RUN_META_END = "BUGPATROL_TRIAGE_RUN_META -->"


@dataclass(frozen=True)
class TriageRunPlan:
    context_path: Path
    schema_path: Path
    output_path: Path
    invocation: AgentInvocation
    context_comment_ids: tuple[str, ...] = ()
    known_assignees: tuple[str, ...] = ()
    # Human-facing note about which branch was analyzed (empty for main), shown
    # in the triage comment header and the Lark notify.
    branch_note: str = ""


def prepare_triage_run(
    *,
    config: ProjectConfig,
    issue_number: int,
    repo_path: Path,
    output_dir: Path,
    github: GitHubCliIssuesClient,
    prompt_path: Path = Path("prompts/triage.zh.md"),
    branch_note: str = "",
) -> TriageRunPlan:
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)
    output_dir.mkdir(parents=True, exist_ok=True)
    comments = github.list_issue_comments(repo=config.github_repo, issue_number=issue_number)
    known_assignees = list_known_assignees(repo_path, config=config)
    context = build_triage_context(
        issue=issue,
        comments=comments,
        prd_root=repo_path / config.prd.cache_path,
        prd_include_globs=config.prd.include_globs,
        roster=build_assignee_roster(
            known_assignees,
            codeowners_identities=load_codeowners_identities(repo_path),
        ),
    )
    context_path = output_dir / "triage-context.md"
    schema_path = output_dir / "triage.schema.json"
    output_path = output_dir / "triage-output.json"
    context_path.write_text(render_triage_context_markdown(context))
    schema_path.write_text(
        json.dumps(
            triage_output_schema(known_assignees=known_assignees),
            ensure_ascii=False,
            indent=2,
        )
    )
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
        context_comment_ids=comment_ids(comments),
        invocation=invocation,
        known_assignees=known_assignees,
        branch_note=branch_note,
    )


def list_known_assignees(repo_path: Path, *, config: ProjectConfig) -> tuple[str, ...]:
    """Return valid GitHub logins for triage assignment.

    Combines CODEOWNERS owners with the [owners] tables in the project config.
    Used both to constrain the agent output schema and to reject results that
    use display names ("Andy") instead of logins ("AndyCokeZero").
    """
    logins: set[str] = set()
    for rule in load_codeowners(repo_path):
        for owner in rule.owners:
            handle = owner.lstrip("@")
            # Team handles (org/team) cannot be issue assignees.
            if handle and "/" not in handle:
                logins.add(handle)
    for group in (config.owners.default, *config.owners.paths.values(), *config.owners.capabilities.values()):
        for owner in group:
            handle = owner.lstrip("@")
            if handle and "/" not in handle:
                logins.add(handle)
    return tuple(sorted(logins))


def build_assignee_roster(
    known_assignees: tuple[str, ...],
    *,
    codeowners_identities: dict[str, tuple[str, ...]] | None = None,
) -> tuple[AssigneeIdentity, ...]:
    """Pair each valid assignee login with its human aliases.

    Lets the triage agent map a free-form "assign to X" reference (an
    @mention, a typed Lark/GitHub name, or a short form) to a login. Aliases
    are the login itself plus the name documented in the CODEOWNERS header.
    """
    codeowners_identities = codeowners_identities or {}
    roster: list[AssigneeIdentity] = []
    for login in known_assignees:
        aliases = tuple(dict.fromkeys((login, *codeowners_identities.get(login, ()))))
        roster.append(AssigneeIdentity(login=login, aliases=aliases))
    return tuple(roster)


def _render_triage_start_message(*, issue_number: int, issue_url: str, branch_note: str = "") -> str:
    text = f"开始分诊，GitHub issue [#{issue_number}]({issue_url})"
    if branch_note:
        text += f"\n{branch_note}"
    runner = triage_runner_name()
    if runner:
        text += f"\n分诊执行机：{runner}"
    return text


def resolve_issue_branch(
    *,
    config: ProjectConfig,
    issue_number: int,
    base_repo: Path,
    github: GitHubCliIssuesClient,
    driver: GitDriver | None = None,
) -> BranchResolution:
    """Resolve which branch/ref a triage run should analyze for an issue.

    Reads the declared branch from the issue's BUGPATROL_INTAKE_META; legacy or
    main-branch issues resolve to main (no worktree needed).
    """
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    metadata = require_bugpatrol_managed_issue(issue)
    driver = driver or SubprocessGitDriver(base_repo)
    return resolve_triage_branch(
        driver,
        target_branch=target_branch_from_metadata(metadata),
        branch_tip_sha=branch_tip_sha_from_metadata(metadata),
    )


def execute_triage_run(
    *,
    config: ProjectConfig,
    issue_number: int,
    plan: TriageRunPlan,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None = None,
    accept_stale_context: bool = False,
    final_attempt: bool = True,
) -> str:
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)
    run_id = str(uuid4())
    mark_triage_running(
        config=config,
        issue_number=issue_number,
        issue_fields=issue_fields,
    )
    if lark is not None:
        send_intake_topic_message(
            repo=config.github_repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            text=_render_triage_start_message(
                issue_number=issue_number,
                issue_url=issue.url,
                branch_note=plan.branch_note,
            ),
        )
    record_triage_run_start(
        config=config,
        issue_number=issue_number,
        plan=plan,
        run_id=run_id,
        github=github,
    )
    agent_env = {**os.environ, **plan.invocation.env} if plan.invocation.env else None
    # stdin must be closed: in CI runners stdin is a pipe that never reaches
    # EOF, and `claude -p` blocks reading it forever after finishing its work.
    started = time.monotonic()
    completed = subprocess.run(
        plan.invocation.command,
        check=False,
        env=agent_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    duration_seconds = time.monotonic() - started
    # Persist the turn-by-turn stream so a failed or surprising run can be
    # analysed on the runner afterwards, and derive token usage from it.
    _write_turn_log(plan.output_path.parent, completed.stdout, completed.stderr)
    input_tokens, cached_input_tokens, output_tokens = parse_claude_token_usage(
        completed.stdout or ""
    )
    run_stats = TriageRunStats(
        duration_seconds=duration_seconds,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        model=plan.invocation.model,
    )
    if completed.returncode != 0:
        mark_triage_failed(
            config=config,
            issue_number=issue_number,
            exit_code=completed.returncode,
            github=github,
            issue_fields=issue_fields,
            lark=lark,
        )
        raise RuntimeError(f"triage agent failed with exit {completed.returncode}")
    if not plan.output_path.exists():
        # Models occasionally end the turn silently without writing the
        # output file; retry instead of failing the run outright.
        if not final_attempt:
            return "no_output"
        mark_triage_failed(
            config=config,
            issue_number=issue_number,
            exit_code=0,
            github=github,
            issue_fields=issue_fields,
            lark=lark,
        )
        raise RuntimeError("triage agent exited 0 but produced no output file")
    result = parse_triage_result(json.loads(plan.output_path.read_text()))
    if plan.known_assignees and result.assignee not in plan.known_assignees:
        mark_triage_failed(
            config=config,
            issue_number=issue_number,
            exit_code=0,
            github=github,
            issue_fields=issue_fields,
            lark=lark,
            reason=(
                f"Agent returned assignee `{result.assignee}`, which is not a known GitHub login. "
                f"Valid assignees: {', '.join(plan.known_assignees)}."
            ),
        )
        raise RuntimeError(f"triage agent returned unknown assignee {result.assignee!r}")
    current_comments = github.list_issue_comments(repo=config.github_repo, issue_number=issue_number)
    if latest_triage_run_id(current_comments) != run_id:
        mark_triage_superseded(
            config=config,
            issue_number=issue_number,
            run_id=run_id,
            github=github,
        )
        return "superseded"
    if comment_ids(current_comments) != plan.context_comment_ids:
        if not accept_stale_context:
            return "stale_context"
        result = annotate_result_stale_context(result)
    apply_triage_result(
        repo=config.github_repo,
        issue_number=issue_number,
        config=config,
        result=result,
        github=github,
        issue_fields=issue_fields,
        lark=lark,
        run_stats=run_stats,
        branch_note=plan.branch_note,
    )
    return "applied"


def _write_turn_log(output_dir: Path, stdout: str | None, stderr: str | None) -> None:
    if stdout:
        (output_dir / "agent-turns.jsonl").write_text(stdout)
    if stderr:
        (output_dir / "agent-stderr.log").write_text(stderr)


def record_triage_run_start(
    *,
    config: ProjectConfig,
    issue_number: int,
    plan: TriageRunPlan,
    run_id: str,
    github: GitHubCliIssuesClient,
) -> None:
    metadata = {
        "version": 1,
        "issue": issue_number,
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "context_comment_ids": list(plan.context_comment_ids),
    }
    github.add_issue_comment(
        repo=config.github_repo,
        issue_number=issue_number,
        body=append_triage_run_metadata(metadata),
    )


def mark_triage_running(
    *,
    config: ProjectConfig,
    issue_number: int,
    issue_fields: GitHubIssueFieldsClient,
) -> None:
    issue_fields.add_issue_field_values(
        repo=config.github_repo,
        issue_number=issue_number,
        values={"Triage status": "Running"},
        config=config,
    )


def mark_triage_superseded(
    *,
    config: ProjectConfig,
    issue_number: int,
    run_id: str,
    github: GitHubCliIssuesClient,
) -> None:
    # The newer run owns the status field; this run only leaves an audit trail.
    github.add_issue_comment(
        repo=config.github_repo,
        issue_number=issue_number,
        body=(
            "## BugPatrol triage skipped\n\n"
            f"Run `{run_id}` was superseded by a newer triage run. Review the latest context before applying results."
        ),
    )


def mark_triage_failed(
    *,
    config: ProjectConfig,
    issue_number: int,
    exit_code: int,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None = None,
    reason: str = "",
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
        body=render_triage_failed_comment(exit_code=exit_code, reason=reason),
    )
    if lark is not None:
        lines = [f"分诊失败，GitHub issue #{issue_number} 已标记 Failed，待重试或人工处理。"]
        if reason:
            lines.append(reason)
        send_intake_topic_message(
            repo=config.github_repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            text="\n".join(lines),
        )


def comment_ids(comments: tuple[GitHubIssueComment, ...]) -> tuple[str, ...]:
    return tuple(comment.id for comment in comments if parse_triage_run_metadata(comment.body) is None)


def append_triage_run_metadata(metadata: dict[str, object]) -> str:
    return (
        f"{TRIAGE_RUN_META_START}\n"
        f"{json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)}\n"
        f"{TRIAGE_RUN_META_END}"
    )


def parse_triage_run_metadata(comment_body: str) -> dict[str, object] | None:
    start = comment_body.find(TRIAGE_RUN_META_START)
    if start == -1:
        return None
    json_start = start + len(TRIAGE_RUN_META_START)
    end = comment_body.find(TRIAGE_RUN_META_END, json_start)
    if end == -1:
        return None
    data = json.loads(comment_body[json_start:end].strip())
    return data if isinstance(data, dict) else None


def latest_triage_run_id(comments: tuple[GitHubIssueComment, ...]) -> str:
    latest = ""
    for comment in comments:
        metadata = parse_triage_run_metadata(comment.body)
        if metadata is not None and isinstance(metadata.get("run_id"), str):
            latest = str(metadata["run_id"])
    return latest


def annotate_result_stale_context(result: TriageResult) -> TriageResult:
    comment = "\n".join(
        [
            result.comment_markdown.rstrip(),
            "",
            "> BugPatrol note: new issue comments kept arriving during triage retries; this result may not reflect the very latest comments.",
        ]
    )
    return replace(result, comment_markdown=comment)


def render_triage_failed_comment(*, exit_code: int, reason: str = "") -> str:
    detail = reason or (
        f"The triage agent exited with code `{exit_code}`.\n"
        "Check the runner logs, credentials, prompt/schema files, and repository checkout."
    )
    return "\n".join(
        [
            "## BugPatrol triage failed",
            "",
            detail,
        ]
    )
