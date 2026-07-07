"""Prepare and optionally execute a triage agent run."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from bugpatrol.agents import AgentInvocation, build_triage_agent_invocation
from bugpatrol.clients import GitHubIssueComment, LarkMessengerClient
from bugpatrol.config import ProjectConfig, branch_matches_patterns
from bugpatrol.fields import triage_output_schema
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import require_bugpatrol_managed_issue
from bugpatrol.intake_workflow import INTAKE_REPLY_META_MARKER
from bugpatrol.ownership import load_codeowners
from bugpatrol.triage_context import build_triage_context, render_triage_context_markdown
from bugpatrol.triage_result import (
    TriageResult,
    apply_triage_result,
    parse_triage_result,
    reject_affected_branch,
    send_intake_topic_message,
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
    known_branches: tuple[str, ...] = ()
    known_assignees: tuple[str, ...] = ()


def prepare_triage_run(
    *,
    config: ProjectConfig,
    issue_number: int,
    repo_path: Path,
    output_dir: Path,
    github: GitHubCliIssuesClient,
    prompt_path: Path = Path("prompts/triage.zh.md"),
) -> TriageRunPlan:
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)
    output_dir.mkdir(parents=True, exist_ok=True)
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
    known_branches = list_matching_repo_branches(repo_path, patterns=config.branches.allowed)
    known_assignees = list_known_assignees(repo_path, config=config)
    schema_path.write_text(
        json.dumps(
            triage_output_schema(
                branch_patterns=config.branches.allowed,
                known_branches=known_branches,
                known_assignees=known_assignees,
            ),
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
        known_branches=known_branches,
        known_assignees=known_assignees,
    )


MAX_KNOWN_BRANCHES = 50


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


def list_matching_repo_branches(repo_path: Path, *, patterns: tuple[str, ...]) -> tuple[str, ...]:
    """Return real branches of the repo checkout that match the allowed patterns.

    Best-effort: when `repo_path` is not a git repository (e.g. a runner that
    only materializes the PRD cache), return () and rely on pattern-based
    validation instead.
    """
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_path),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
            "refs/remotes/origin",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ()
    branches: list[str] = []
    for line in completed.stdout.splitlines():
        name = line.strip()
        if name.startswith("origin/"):
            name = name[len("origin/") :]
        if not name or name == "HEAD" or name in branches:
            continue
        if branch_matches_patterns(name, patterns):
            branches.append(name)
    return tuple(sorted(branches)[:MAX_KNOWN_BRANCHES])


def execute_triage_run(
    *,
    config: ProjectConfig,
    issue_number: int,
    plan: TriageRunPlan,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None = None,
) -> None:
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
            text=f"开始分诊，GitHub issue #{issue_number}: {issue.url}",
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
    completed = subprocess.run(plan.invocation.command, check=False, env=agent_env, stdin=subprocess.DEVNULL)
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
        mark_triage_failed(
            config=config,
            issue_number=issue_number,
            exit_code=0,
            github=github,
            issue_fields=issue_fields,
            lark=lark,
        )
        raise RuntimeError("triage agent exited 0 but produced no output file")
    result = parse_triage_result(
        json.loads(plan.output_path.read_text()),
        branch_patterns=config.branches.allowed,
    )
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
    if (
        result.affected_branch
        and plan.known_branches
        and result.affected_branch not in plan.known_branches
    ):
        # Pattern-valid but nonexistent branch (agent fabrication): demote it
        # to a visible rejected value instead of recording false data.
        result = reject_affected_branch(result)
    current_comments = github.list_issue_comments(repo=config.github_repo, issue_number=issue_number)
    if latest_triage_run_id(current_comments) != run_id:
        mark_triage_superseded(
            config=config,
            issue_number=issue_number,
            run_id=run_id,
            github=github,
            issue_fields=issue_fields,
        )
        return
    if comment_ids(current_comments) != plan.context_comment_ids:
        result = mark_result_needs_review(result)
    if not result.affected_branch:
        # Reporters answer the branch question with a bare branch name in the
        # topic; resolve it deterministically instead of trusting the agent.
        branch_answer = branch_answer_from_comments(
            current_comments, known_branches=plan.known_branches
        )
        if branch_answer:
            result = replace(result, affected_branch=branch_answer, affected_branch_rejected="")
    apply_triage_result(
        repo=config.github_repo,
        issue_number=issue_number,
        config=config,
        result=result,
        github=github,
        issue_fields=issue_fields,
        lark=lark,
    )


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
    issue_fields: GitHubIssueFieldsClient,
) -> None:
    issue_fields.add_issue_field_values(
        repo=config.github_repo,
        issue_number=issue_number,
        values={"Triage status": "Needs review"},
        config=config,
    )
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


def branch_answer_from_comments(
    comments: tuple[GitHubIssueComment, ...],
    *,
    known_branches: tuple[str, ...],
) -> str:
    """Deterministically extract an affected-branch answer from topic replies.

    When the bot asks for the affected branch in the Lark topic, reporters
    typically reply with just the branch name. That reply is appended to the
    issue as a follow-up comment; if its message section is exactly a known
    branch name, use it directly instead of relying on the agent to notice.
    """
    answer = ""
    for comment in comments:
        body = comment.body or ""
        if INTAKE_REPLY_META_MARKER not in body:
            continue
        text = _followup_message_text(body).strip().strip("`")
        if text in known_branches:
            answer = text  # keep scanning: the latest reply wins
    return answer


def _followup_message_text(body: str) -> str:
    lines = body.splitlines()
    collected: list[str] = []
    in_message = False
    for line in lines:
        if line.strip() in ("## 消息", "## Message"):
            in_message = True
            continue
        if in_message and line.startswith("## "):
            break
        if in_message:
            collected.append(line)
    return "\n".join(collected)


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


def mark_result_needs_review(result: TriageResult) -> TriageResult:
    fields = dict(result.fields)
    fields["Triage status"] = "Needs review"
    comment = "\n".join(
        [
            result.comment_markdown.rstrip(),
            "",
            "> BugPatrol note: new issue comments arrived after this triage context was generated. Review before treating this result as final.",
        ]
    )
    return replace(result, fields=fields, comment_markdown=comment)


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
