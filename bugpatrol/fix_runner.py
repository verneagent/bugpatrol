"""Prepare and execute an auto-fix agent run.

Mirrors triage_runner but for the fix lifecycle: read the confirmed triage
verdict/root-cause, edit code in an ephemeral branch worktree, gate the *real*
git diff, run the project's own verify commands, then open a PR (never merge).

Every failure path here is a hard stop that does not open a PR: a bad verdict,
a protected/oversized diff, or a failing verify command all short-circuit to a
blocked/verify-failed notification and return without landing anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from bugpatrol.agents import (
    AgentInvocation,
    build_fix_agent_invocation,
    detect_sandbox_denial,
    parse_claude_token_usage,
)
from bugpatrol.clients import (
    GitHubIssue,
    GitHubIssueComment,
    LarkMessengerClient,
    OpenPullRequest,
    ReviewThread,
)
from bugpatrol.config import ProjectConfig
from bugpatrol.fields import fix_output_schema
from bugpatrol.fix_gate import (
    evaluate_post_edit,
    evaluate_triage_readiness,
    run_verify_commands,
    verify_all_passed,
)
from bugpatrol.fix_result import (
    FixResult,
    build_pr_body,
    fix_result_fingerprint,
    notify_conflict_escalation,
    notify_fix_pr,
    notify_fix_revise,
    parse_fix_result,
    render_baseline_broken_comment,
    render_baseline_broken_lark_message,
    render_blocked_comment,
    render_conflict_instructions_markdown,
    render_fix_blocked_lark_message,
    render_review_feedback_markdown,
    render_verify_failed_comment,
    render_verify_failed_lark_message,
)
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import parse_intake_metadata, require_bugpatrol_managed_issue
from bugpatrol.lark import is_message_withdrawn_error
from bugpatrol.progress import ProgressReporter
from bugpatrol.triage_result import (
    TriageRunStats,
    parse_triage_metadata,
    send_intake_topic_message,
    triage_runner_name,
)
from bugpatrol.triage_runner import resolve_issue_branch
from bugpatrol.worktree import (
    fix_revise_worktree,
    fix_worktree,
    worktree_changed_files,
    worktree_commit_all,
    worktree_diff_line_count,
    worktree_merge_abort,
    worktree_merge_base,
    worktree_push_branch,
    worktree_reset_to_head,
    worktree_unresolved_conflict_markers,
)


@dataclass(frozen=True)
class FixRunPlan:
    context_path: Path
    schema_path: Path
    output_path: Path
    invocation: AgentInvocation
    # The fix branch worktree the agent edits (and where the diff is measured).
    agent_cwd: Path
    verdict: str
    # Branch the PR targets (the branch triage analyzed: main or a feature branch).
    base_branch: str
    # New fix branch pushed and used as PR head.
    head_branch: str
    reviewer: str
    reviewer_open_id: str
    branch_note: str


def read_triage_verdict(
    *,
    config: ProjectConfig,
    issue_number: int,
    issue_fields: GitHubIssueFieldsClient,
) -> str:
    """The current Triage verdict field value ("" when unset)."""
    values = issue_fields.get_issue_field_values(repo=config.github_repo, issue_number=issue_number)
    github_name = config.issue_field_names.get("Triage verdict", "Triage verdict")
    return values.get(github_name, "")


def latest_triage_analysis(comments: Sequence[GitHubIssueComment]) -> str:
    """Markdown of the most recent applied triage comment (root cause etc.).

    Fix relies on triage's confirmed analysis; the last comment carrying a
    BUGPATROL_TRIAGE_META marker is the authoritative verdict comment.
    """
    latest = ""
    for comment in comments:
        if parse_triage_metadata(comment.body) is not None:
            latest = comment.body
    return latest


def render_fix_context_markdown(
    *,
    issue: GitHubIssue,
    verdict: str,
    triage_analysis: str,
    branch_note: str,
) -> str:
    lines = [
        f"# Fix context — issue #{issue.number}",
        "",
        f"标题：{issue.title}",
        f"链接：{issue.url}",
        f"分诊结论（Triage verdict）：{verdict}",
    ]
    if branch_note:
        lines.append(f"分支范围：{branch_note}")
    lines.extend(
        [
            "",
            "## 上游 triage 的确认分析（根因 / 复现 / 涉及位置 / 负责人）",
            "",
            triage_analysis.strip() or "（无 triage 分析评论，禁止在此情况下修复）",
            "",
            "## 原始 issue 正文",
            "",
            (issue.body or "").strip(),
        ]
    )
    return "\n".join(lines)


def prepare_fix_run(
    *,
    config: ProjectConfig,
    issue_number: int,
    worktree_path: Path,
    output_dir: Path,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    base_branch: str,
    head_branch: str,
    branch_note: str = "",
    prompt_path: Path = Path("prompts/fix.zh.md"),
) -> FixRunPlan:
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)
    output_dir.mkdir(parents=True, exist_ok=True)
    # The agent runs with cwd=worktree, so every path handed to it must be
    # absolute (a relative path would resolve against the checkout and vanish).
    output_dir = output_dir.resolve()
    prompt_path = prompt_path.resolve()
    worktree_path = worktree_path.resolve()
    verdict = read_triage_verdict(config=config, issue_number=issue_number, issue_fields=issue_fields)
    comments = github.list_issue_comments(repo=config.github_repo, issue_number=issue_number)
    triage_analysis = latest_triage_analysis(comments)
    context_path = output_dir / "fix-context.md"
    schema_path = output_dir / "fix.schema.json"
    output_path = output_dir / "fix-output.json"
    context_path.write_text(
        render_fix_context_markdown(
            issue=issue,
            verdict=verdict,
            triage_analysis=triage_analysis,
            branch_note=branch_note,
        )
    )
    schema_path.write_text(json.dumps(fix_output_schema(), ensure_ascii=False, indent=2))
    invocation = build_fix_agent_invocation(
        config,
        issue_number=issue_number,
        prompt_path=prompt_path,
        schema_path=schema_path,
        output_path=output_path,
        context_path=context_path,
        # Re-admit the runner-side workspace (prompt + output dir) that sits
        # outside the branch worktree the agent is cwd'd into.
        workspace_dirs=(prompt_path.parent, output_dir),
    )
    reviewer = issue.assignees[0] if issue.assignees else ""
    reviewer_open_id = (config.lark.user_open_ids or {}).get(reviewer, "") if reviewer else ""
    return FixRunPlan(
        context_path=context_path,
        schema_path=schema_path,
        output_path=output_path,
        invocation=invocation,
        agent_cwd=worktree_path,
        verdict=verdict,
        base_branch=base_branch,
        head_branch=head_branch,
        reviewer=reviewer,
        reviewer_open_id=reviewer_open_id,
        branch_note=branch_note,
    )


def run_fix(
    *,
    config: ProjectConfig,
    issue_number: int,
    base_repo: Path,
    output_dir: Path,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None = None,
    prompt_path: Path = Path("prompts/fix.zh.md"),
) -> str:
    """Full auto-fix lifecycle for one issue; returns a terminal status string.

    Statuses: not_fixable, already_open_pr, blocked, verify_failed,
    baseline_broken, no_changes, no_output, opened_pr.
    """
    if config.fix is None:
        raise ValueError("project config has no [fix] table; auto-fix is not enabled")
    fix = config.fix
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)

    verdict = read_triage_verdict(config=config, issue_number=issue_number, issue_fields=issue_fields)
    readiness = evaluate_triage_readiness(verdict=verdict, fix=fix)
    if not readiness.allowed:
        _post_blocked(
            config=config,
            issue=issue,
            reason=readiness.reason,
            github=github,
            lark=lark,
        )
        return "not_fixable"

    head_branch = fix.branch_for_issue(issue_number)
    existing_pr = github.find_open_pull_request_by_head(repo=config.github_repo, head=head_branch)
    if existing_pr:
        # Idempotency: a prior fix run already opened a PR for this issue.
        return "already_open_pr"

    resolution = resolve_issue_branch(
        config=config,
        issue_number=issue_number,
        base_repo=base_repo,
        github=github,
    )
    with fix_worktree(base_repo=base_repo, ref=resolution.ref, branch=head_branch) as worktree:
        plan = prepare_fix_run(
            config=config,
            issue_number=issue_number,
            worktree_path=worktree,
            output_dir=output_dir,
            github=github,
            issue_fields=issue_fields,
            base_branch=resolution.analyzed_branch,
            head_branch=head_branch,
            branch_note=resolution.note,
            prompt_path=prompt_path,
        )
        return execute_fix_run(
            config=config,
            issue=issue,
            plan=plan,
            github=github,
            lark=lark,
        )


def prepare_fix_revise(
    *,
    config: ProjectConfig,
    issue_number: int,
    worktree_path: Path,
    output_dir: Path,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    head_branch: str,
    threads: Sequence[ReviewThread],
    base_branch: str = "",
    conflict_files: Sequence[str] = (),
    prompt_path: Path = Path("prompts/fix.zh.md"),
) -> FixRunPlan:
    """Build a revise plan: the fix context plus the PR's unresolved feedback.

    Reuses prepare_fix_run (same triage root cause + schema + invocation) and
    appends any merge-conflict instructions (resolve first) and the review
    threads so the agent addresses everything in place. `base_branch` here is
    the PR's target branch, used only to word the conflict instructions.
    """
    plan = prepare_fix_run(
        config=config,
        issue_number=issue_number,
        worktree_path=worktree_path,
        output_dir=output_dir,
        github=github,
        issue_fields=issue_fields,
        # No PR is opened on revise; base_branch/branch_note are unused here.
        base_branch="",
        head_branch=head_branch,
        branch_note="",
        prompt_path=prompt_path,
    )
    sections: list[str] = []
    if conflict_files:
        sections.append(
            render_conflict_instructions_markdown(
                base_branch=base_branch, files=tuple(conflict_files)
            )
        )
    if threads:
        sections.append(render_review_feedback_markdown(tuple(threads)))
    if sections:
        with plan.context_path.open("a") as handle:
            handle.write("\n\n" + "\n\n".join(sections) + "\n")
    return plan


def run_fix_revise(
    *,
    config: ProjectConfig,
    issue_number: int,
    base_repo: Path,
    output_dir: Path,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None = None,
    prompt_path: Path = Path("prompts/fix.zh.md"),
) -> str:
    """Address open-PR review feedback / target-branch conflicts on a fix.

    Stateless like run_fix: it rebuilds everything from origin (the fix branch)
    and GitHub (the PR's unresolved review threads + mergeability), so it may run
    on a different runner than the run that opened the PR. When the PR conflicts
    with its target branch it merges the target in (never force-push) and lets
    the agent resolve the markers, escalating to a human if too many files
    conflict. Statuses: no_open_pr, no_feedback, conflict_escalated,
    conflict_unresolved, conflict_resolved, blocked, no_changes, no_output,
    verify_failed, revised.
    """
    if config.fix is None:
        raise ValueError("project config has no [fix] table; auto-fix is not enabled")
    fix = config.fix
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)

    head_branch = fix.branch_for_issue(issue_number)
    pr = github.get_open_pull_request_by_head(repo=config.github_repo, head=head_branch)
    if pr is None:
        # Nothing to revise: no open fix PR for this issue.
        return "no_open_pr"
    threads = github.list_unresolved_review_threads(repo=config.github_repo, pr_number=pr.number)
    has_conflict = pr.mergeable.upper() == "CONFLICTING"
    if not threads and not has_conflict:
        # Stateless no-op: no unresolved feedback and the PR merges cleanly.
        return "no_feedback"

    with fix_revise_worktree(base_repo=base_repo, branch=head_branch) as worktree:
        conflict_files: tuple[str, ...] = ()
        reviewer = issue.assignees[0] if issue.assignees else ""
        reviewer_open_id = (config.lark.user_open_ids or {}).get(reviewer, "") if reviewer else ""
        if has_conflict:
            if not pr.base_ref:
                # We can't merge the target in without knowing it; hand off.
                notify_conflict_escalation(
                    repo=config.github_repo,
                    issue_number=issue.number,
                    issue_url=issue.url,
                    pr_url=pr.url,
                    base_branch="(unknown)",
                    files=(),
                    github=github,
                    lark=lark,
                    reviewer_open_id=reviewer_open_id,
                )
                return "conflict_escalated"
            merge = worktree_merge_base(worktree, base_branch=pr.base_ref)
            if merge.status == "conflict":
                if len(merge.conflicted_files) > fix.max_conflict_files:
                    worktree_merge_abort(worktree)
                    notify_conflict_escalation(
                        repo=config.github_repo,
                        issue_number=issue.number,
                        issue_url=issue.url,
                        pr_url=pr.url,
                        base_branch=pr.base_ref,
                        files=merge.conflicted_files,
                        github=github,
                        lark=lark,
                        reviewer_open_id=reviewer_open_id,
                    )
                    return "conflict_escalated"
                conflict_files = merge.conflicted_files
            # A clean merge already made a merge commit; nothing for the agent to
            # resolve, so conflict_files stays empty.
        plan = prepare_fix_revise(
            config=config,
            issue_number=issue_number,
            worktree_path=worktree,
            output_dir=output_dir,
            github=github,
            issue_fields=issue_fields,
            head_branch=head_branch,
            threads=threads,
            base_branch=pr.base_ref,
            conflict_files=conflict_files,
            prompt_path=prompt_path,
        )
        return execute_fix_revise(
            config=config,
            issue=issue,
            plan=plan,
            pr=pr,
            threads=threads,
            github=github,
            lark=lark,
            has_conflict=has_conflict,
            conflict_files=conflict_files,
        )


def execute_fix_revise(
    *,
    config: ProjectConfig,
    issue: GitHubIssue,
    plan: FixRunPlan,
    pr: OpenPullRequest,
    threads: Sequence[ReviewThread],
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None = None,
    has_conflict: bool = False,
    conflict_files: Sequence[str] = (),
) -> str:
    """Run the revise agent (feedback and/or conflict resolution), then push.

    When `has_conflict` the worktree already carries the target branch merged in
    (done by run_fix_revise). The agent — if there is any work: unresolved
    conflict markers and/or review threads — resolves it in place. The diff-size/
    protected-path gate is skipped on the conflict path because a legitimate
    target-branch merge changes many files outside the fix; verify commands are
    the real gate there. On a plain feedback revise the gate still applies.
    """
    fix = config.fix
    assert fix is not None  # run_fix_revise guards this.
    needs_agent = bool(threads) or bool(conflict_files)
    if lark is not None:
        send_intake_topic_message(
            repo=config.github_repo,
            issue_number=issue.number,
            github=github,
            lark=lark,
            text=_render_revise_start_message(
                issue_number=issue.number,
                issue_url=issue.url,
                feedback_count=len(threads),
                base_branch=pr.base_ref if has_conflict else "",
            ),
        )
    run_stats = _run_fix_agent(plan) if needs_agent else None

    # Deterministic guard: the agent must have removed every conflict marker it
    # was handed, or a blind `git add -A` would commit the markers.
    if conflict_files:
        leftover = worktree_unresolved_conflict_markers(plan.agent_cwd, tuple(conflict_files))
        if leftover:
            _post_blocked(
                config=config,
                issue=issue,
                reason=("冲突未完全解决，以下文件仍有冲突标记：" + ", ".join(leftover)),
                github=github,
                lark=lark,
            )
            return "conflict_unresolved"

    changed_files = worktree_changed_files(plan.agent_cwd)
    if not has_conflict:
        diff_line_count = worktree_diff_line_count(plan.agent_cwd)
        gate = evaluate_post_edit(
            changed_files=changed_files, diff_line_count=diff_line_count, fix=fix
        )
        if not gate.allowed:
            _post_blocked(config=config, issue=issue, reason=gate.reason, github=github, lark=lark)
            return "no_changes" if not changed_files else "blocked"

    if needs_agent:
        if not plan.output_path.exists():
            _post_blocked(
                config=config,
                issue=issue,
                reason="revise agent edited code but wrote no output JSON summary",
                github=github,
                lark=lark,
            )
            return "no_output"
        result = parse_fix_result(json.loads(plan.output_path.read_text()))
    else:
        # Clean auto-merge with no feedback: no agent ran, so synthesize a
        # summary for the notification (the merge commit is the only change).
        result = FixResult(
            summary=f"合并目标分支 `{pr.base_ref}` 以解决与目标分支的冲突",
            root_cause=f"目标分支 `{pr.base_ref}` 前移，PR 与其冲突",
            tests_added=False,
            pr_title="",
            pr_body="",
        )

    verify_outcomes = run_verify_commands(fix=fix, cwd=plan.agent_cwd)
    if not verify_all_passed(verify_outcomes):
        _post_verify_failed(
            config=config, issue=issue, verify_outcomes=verify_outcomes, github=github, lark=lark
        )
        return "verify_failed"

    if changed_files:
        worktree_commit_all(plan.agent_cwd, message=_revise_commit_message(issue.number, has_conflict, threads, pr.base_ref))
    # Fast-forward append to the existing remote fix branch, updating the PR.
    worktree_push_branch(plan.agent_cwd, branch=plan.head_branch)
    # Notify BEFORE resolving threads (Lark-first, marker-last): resolving is the
    # dedup marker, so a failed notification must leave threads unresolved for an
    # at-least-once retry rather than silently marking the feedback handled.
    notify_fix_revise(
        repo=config.github_repo,
        issue_number=issue.number,
        issue_url=issue.url,
        pr_url=pr.url,
        result=result,
        addressed=len(threads),
        github=github,
        lark=lark,
        reviewer_open_id=plan.reviewer_open_id,
        run_stats=run_stats,
        conflicted=has_conflict,
        base_branch=pr.base_ref,
    )
    for thread in threads:
        github.resolve_review_thread(thread_id=thread.id)
    if threads:
        return "revised"
    return "conflict_resolved"


def _revise_commit_message(
    issue_number: int, has_conflict: bool, threads: Sequence[ReviewThread], base_branch: str
) -> str:
    if has_conflict and threads:
        return f"fix: merge {base_branch} and address review feedback (#{issue_number})"
    if has_conflict:
        return f"fix: merge {base_branch} to resolve conflicts (#{issue_number})"
    return f"fix: address review feedback (#{issue_number})"


def _run_fix_agent(plan: FixRunPlan) -> TriageRunStats:
    """Run the fix/revise agent subprocess and return its run stats.

    Shared by execute_fix_run and execute_fix_revise: same command, cwd, stdin
    handling, turn-log persistence, and hard stops on nonzero exit / sandbox
    denial.
    """
    agent_env = {**os.environ, **plan.invocation.env} if plan.invocation.env else None
    started = time.monotonic()
    completed = subprocess.run(
        plan.invocation.command,
        check=False,
        env=agent_env,
        cwd=str(plan.agent_cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    duration_seconds = time.monotonic() - started
    _write_turn_log(plan.output_path.parent, completed.stdout, completed.stderr)
    input_tokens, cached_input_tokens, output_tokens = parse_claude_token_usage(completed.stdout or "")
    if completed.returncode != 0:
        raise RuntimeError(f"fix agent failed with exit {completed.returncode}")
    denial = detect_sandbox_denial(completed.stdout or "")
    if denial:
        raise RuntimeError(f"fix agent blocked by sandbox/permission denial: {denial}")
    return TriageRunStats(
        duration_seconds=duration_seconds,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        model=plan.invocation.model,
    )


def execute_fix_run(
    *,
    config: ProjectConfig,
    issue: GitHubIssue,
    plan: FixRunPlan,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None = None,
) -> str:
    fix = config.fix
    assert fix is not None  # run_fix guards this; kept for callers using execute directly.
    reporter = _build_progress_reporter(config=config, issue=issue, lark=lark)
    if lark is not None:
        send_intake_topic_message(
            repo=config.github_repo,
            issue_number=issue.number,
            github=github,
            lark=lark,
            text=_render_fix_start_message(issue_number=issue.number, issue_url=issue.url, branch_note=plan.branch_note),
        )
    reporter.set_phase("agent 正在编辑代码")
    reporter.start()
    try:
        run_stats = _run_fix_agent(plan)

        # The gate trusts the real working-tree diff, never the agent's self-report.
        changed_files = worktree_changed_files(plan.agent_cwd)
        diff_line_count = worktree_diff_line_count(plan.agent_cwd)
        gate = evaluate_post_edit(changed_files=changed_files, diff_line_count=diff_line_count, fix=fix)
        if not gate.allowed:
            _post_blocked(config=config, issue=issue, reason=gate.reason, github=github, lark=lark)
            return "no_changes" if not changed_files else "blocked"

        if not plan.output_path.exists():
            # The agent edited code but never wrote its JSON summary; without it we
            # can't build a trustworthy PR body, so treat like a blocked run.
            _post_blocked(
                config=config,
                issue=issue,
                reason="fix agent edited code but wrote no output JSON summary",
                github=github,
                lark=lark,
            )
            return "no_output"
        result = parse_fix_result(json.loads(plan.output_path.read_text()))

        reporter.set_phase("跑验证门（安装依赖 + preflight）")
        status, verify_outcomes = _verify_with_baseline_attribution(fix=fix, worktree=plan.agent_cwd)
        if status == "baseline_broken":
            _post_baseline_broken(
                config=config,
                issue=issue,
                base_branch=plan.base_branch,
                verify_outcomes=verify_outcomes,
                github=github,
                lark=lark,
            )
            return "baseline_broken"
        if status == "fix_failed":
            _post_verify_failed(
                config=config,
                issue=issue,
                verify_outcomes=verify_outcomes,
                github=github,
                lark=lark,
            )
            return "verify_failed"

        reporter.set_phase("提交并开 PR")
        commit_message = f"fix: {result.pr_title} (#{issue.number})"
        worktree_commit_all(plan.agent_cwd, message=commit_message)
        worktree_push_branch(plan.agent_cwd, branch=plan.head_branch)
        pr_url = github.create_pull_request(
            repo=config.github_repo,
            head=plan.head_branch,
            base=plan.base_branch,
            title=result.pr_title,
            body=build_pr_body(
                result=result,
                issue_number=issue.number,
                issue_url=issue.url,
                changed_files=changed_files,
                verify_outcomes=verify_outcomes,
            ),
        )
    finally:
        # Terminal notifications (below and in the _post_* paths) supersede the
        # heartbeat, so always tear the thread down on the way out.
        reporter.stop()
    if plan.reviewer:
        # A non-collaborator or self-review reviewer must not fail a landed PR;
        # the PR already exists, so log the miss and move on rather than raise.
        try:
            github.add_pull_request_reviewer(repo=config.github_repo, pr=pr_url, reviewer=plan.reviewer)
        except Exception as error:
            print(f"bugpatrol: could not request review from {plan.reviewer!r}: {error}", file=sys.stderr)
    fingerprint = fix_result_fingerprint(issue_number=issue.number, changed_files=changed_files)
    notify_fix_pr(
        repo=config.github_repo,
        issue_number=issue.number,
        issue_url=issue.url,
        pr_url=pr_url,
        result=result,
        fingerprint=fingerprint,
        github=github,
        lark=lark,
        reviewer_open_id=plan.reviewer_open_id,
        run_stats=run_stats,
    )
    return "opened_pr"


def _render_fix_start_message(*, issue_number: int, issue_url: str, branch_note: str = "") -> str:
    text = f"开始自动修复，GitHub issue [#{issue_number}]({issue_url})"
    if branch_note:
        text += f"\n{branch_note}"
    runner = triage_runner_name()
    if runner:
        text += f"\n修复执行机：{runner}"
    return text


def _build_progress_reporter(
    *, config: ProjectConfig, issue: GitHubIssue, lark: LarkMessengerClient | None
) -> ProgressReporter:
    """Heartbeat reporter wired to the issue's Lark topic; inert when unconfigured.

    chat_id/message_id come from the issue body's intake meta (no extra GitHub
    round-trip). When lark is None, the meta is missing, or the configured
    interval is 0, the reporter is simply disabled and its ``start()`` is a
    no-op — the run behaves exactly as before.
    """
    fix = config.fix
    assert fix is not None
    metadata = parse_intake_metadata(issue.body or "") or {}
    return ProgressReporter(
        replier=lark,
        chat_id=str(metadata.get("chat_id") or ""),
        message_id=str(metadata.get("message_id") or ""),
        issue_number=issue.number,
        interval_seconds=float(fix.progress_heartbeat_seconds),
        runner=triage_runner_name(),
        swallow=is_message_withdrawn_error,
    )


def _render_revise_start_message(
    *, issue_number: int, issue_url: str, feedback_count: int, base_branch: str = ""
) -> str:
    if base_branch and feedback_count:
        head = f"开始解决与 `{base_branch}` 的冲突并按评审反馈更新修复（{feedback_count} 条）"
    elif base_branch:
        head = f"开始合并目标分支 `{base_branch}` 解决冲突"
    else:
        head = f"开始按评审反馈更新修复（{feedback_count} 条）"
    text = f"{head}，GitHub issue [#{issue_number}]({issue_url})"
    runner = triage_runner_name()
    if runner:
        text += f"\n修复执行机：{runner}"
    return text


def _post_blocked(
    *,
    config: ProjectConfig,
    issue: GitHubIssue,
    reason: str,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
) -> None:
    if lark is not None:
        send_intake_topic_message(
            repo=config.github_repo,
            issue_number=issue.number,
            github=github,
            lark=lark,
            text=render_fix_blocked_lark_message(
                issue_number=issue.number,
                issue_url=issue.url,
                reason=reason,
            ),
        )
    github.add_issue_comment(
        repo=config.github_repo,
        issue_number=issue.number,
        body=render_blocked_comment(reason=reason),
    )


def _post_verify_failed(
    *,
    config: ProjectConfig,
    issue: GitHubIssue,
    verify_outcomes,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
) -> None:
    if lark is not None:
        send_intake_topic_message(
            repo=config.github_repo,
            issue_number=issue.number,
            github=github,
            lark=lark,
            text=render_verify_failed_lark_message(
                issue_number=issue.number,
                issue_url=issue.url,
                verify_outcomes=verify_outcomes,
            ),
        )
    github.add_issue_comment(
        repo=config.github_repo,
        issue_number=issue.number,
        body=render_verify_failed_comment(verify_outcomes=verify_outcomes),
    )


def _verify_with_baseline_attribution(*, fix, worktree: Path):
    """Run the verify gate and, on failure, attribute it.

    A failing gate could mean the fix is wrong OR that the target branch is
    already red (in which case blaming the fix is misleading and we should not
    keep grinding). So when the post-fix run fails, reset the worktree back to
    the untouched base HEAD (keeping node_modules) and run the SAME gate again:
    if the pristine baseline also fails, the failure is not the fix's fault.

    Returns ``(status, outcomes)`` where status is:
      - "passed"          — outcomes is the successful post-fix run
      - "fix_failed"      — outcomes is the failing post-fix run (baseline is green)
      - "baseline_broken" — outcomes is the failing baseline run
    """
    outcomes = run_verify_commands(fix=fix, cwd=worktree)
    if verify_all_passed(outcomes):
        return "passed", outcomes
    worktree_reset_to_head(worktree)
    baseline = run_verify_commands(fix=fix, cwd=worktree)
    if not verify_all_passed(baseline):
        return "baseline_broken", baseline
    return "fix_failed", outcomes


def _post_baseline_broken(
    *,
    config: ProjectConfig,
    issue: GitHubIssue,
    base_branch: str,
    verify_outcomes,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
) -> None:
    if lark is not None:
        send_intake_topic_message(
            repo=config.github_repo,
            issue_number=issue.number,
            github=github,
            lark=lark,
            text=render_baseline_broken_lark_message(
                issue_number=issue.number,
                issue_url=issue.url,
                base_branch=base_branch,
                verify_outcomes=verify_outcomes,
            ),
        )
    github.add_issue_comment(
        repo=config.github_repo,
        issue_number=issue.number,
        body=render_baseline_broken_comment(
            base_branch=base_branch, verify_outcomes=verify_outcomes
        ),
    )


def _write_turn_log(output_dir: Path, stdout: str | None, stderr: str | None) -> None:
    if stdout:
        (output_dir / "agent-turns.jsonl").write_text(stdout)
    if stderr:
        (output_dir / "agent-stderr.log").write_text(stderr)
