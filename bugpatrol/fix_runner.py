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
    run_setup_commands,
    run_verify_commands,
    verify_all_passed,
)
from bugpatrol.fix_result import (
    FixResult,
    append_ci_fix_metadata,
    build_pr_body,
    extract_build_links,
    fix_result_fingerprint,
    latest_ci_fix_meta,
    notify_build_links_followup,
    notify_build_ready,
    notify_ci_escalation,
    notify_ci_fix,
    notify_conflict_escalation,
    notify_fix_pr,
    notify_fix_revise,
    notify_pr_ci_failure,
    parse_fix_result,
    render_baseline_broken_comment,
    render_baseline_broken_lark_message,
    render_blocked_comment,
    render_ci_fix_feedback_markdown,
    render_conflict_instructions_markdown,
    render_fix_blocked_lark_message,
    render_reporter_feedback_markdown,
    render_review_feedback_markdown,
    render_verify_failed_comment,
    render_verify_failed_lark_message,
    render_verify_fix_feedback_markdown,
)
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import (
    is_bugpatrol_managed_issue,
    parse_intake_metadata,
    require_bugpatrol_managed_issue,
)
from bugpatrol.intake_workflow import parse_intake_reply_metadata
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


def latest_reporter_correction(comments: Sequence[GitHubIssueComment]) -> str:
    """The reporter's most recent material follow-up on the issue ("" if none).

    Once a fix PR is open, a reporter follow-up classified `material_followup`
    (not an ack / fix-status chatter — see classify_triage_signal) means the fix
    needs to change. Comments are chronological, so the last such intake reply is
    the reporter's current word; the meta footer is stripped so only the
    human-readable update reaches the revise agent.
    """
    latest = ""
    for comment in comments:
        meta = parse_intake_reply_metadata(comment.body)
        if meta is None or meta.get("signal_reason") != "material_followup":
            continue
        latest = _strip_intake_reply_meta(comment.body)
    return latest


def _strip_intake_reply_meta(body: str) -> str:
    """Drop the trailing `--- <!-- BUGPATROL_INTAKE_REPLY_META:... -->` footer."""
    marker = "<!-- BUGPATROL_INTAKE_REPLY_META:"
    index = body.find(marker)
    if index == -1:
        return body.strip()
    return body[:index].rstrip().removesuffix("---").rstrip()


def render_fix_context_markdown(
    *,
    issue: GitHubIssue,
    verdict: str,
    triage_analysis: str,
    branch_note: str,
    self_check_commands: dict[str, str] | None = None,
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
    if self_check_commands:
        lines.extend(
            [
                "",
                "## 自检命令（改完必须自己跑到全绿再收工）",
                "",
                "工作区已装好依赖。改完代码后，你**必须**在当前目录亲自运行下面的验证命令，"
                "看到类型/编译/测试报错就继续改，迭代到全部通过再收工——不要交出编译不过的代码。"
                "迭代时可以先跑更快的子集（例如 `npm run typecheck` 之类项目文档里记录的轻量检查）"
                "定位问题，最后至少确保这些命令全绿：",
                "",
            ]
        )
        for label, command in self_check_commands.items():
            lines.append(f"- {label}：`{command}`")
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
            # Tell the agent the exact bar it must clear itself before finishing.
            self_check_commands=dict(config.fix.verify) if config.fix else None,
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

    Statuses: not_fixable, issue_closed, already_open_pr, blocked, verify_failed,
    setup_failed, baseline_broken, no_changes, no_output, opened_pr.
    """
    if config.fix is None:
        raise ValueError("project config has no [fix] table; auto-fix is not enabled")
    fix = config.fix
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)
    if issue.state == "closed":
        # A closed issue must not be auto-fixed. Reopen it to resume.
        print(f"issue #{issue_number} is closed; skipping auto-fix", file=sys.stderr)
        return "issue_closed"

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
    reporter_feedback: str = "",
    prompt_path: Path = Path("prompts/fix.zh.md"),
) -> FixRunPlan:
    """Build a revise plan: the fix context plus the PR's unresolved feedback.

    Reuses prepare_fix_run (same triage root cause + schema + invocation) and
    appends any merge-conflict instructions (resolve first), the review threads,
    and the reporter's latest material correction so the agent addresses
    everything in place. `base_branch` here is the PR's target branch, used only
    to word the conflict instructions.
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
    if reporter_feedback:
        sections.append(render_reporter_feedback_markdown(reporter_feedback))
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
    conflict. Statuses: no_open_pr, issue_closed, no_feedback, conflict_escalated,
    conflict_unresolved, conflict_resolved, blocked, no_changes, no_output,
    setup_failed, verify_failed, revised.
    """
    if config.fix is None:
        raise ValueError("project config has no [fix] table; auto-fix is not enabled")
    fix = config.fix
    issue = github.get_issue(repo=config.github_repo, issue_number=issue_number)
    require_bugpatrol_managed_issue(issue)
    if issue.state == "closed":
        # A closed issue must not be auto-revised. Reopen it to resume.
        print(f"issue #{issue_number} is closed; skipping fix revise", file=sys.stderr)
        return "issue_closed"

    head_branch = fix.branch_for_issue(issue_number)
    pr = github.get_open_pull_request_by_head(repo=config.github_repo, head=head_branch)
    if pr is None:
        # Nothing to revise: no open fix PR for this issue.
        return "no_open_pr"
    threads = github.list_unresolved_review_threads(repo=config.github_repo, pr_number=pr.number)
    has_conflict = pr.mergeable.upper() == "CONFLICTING"
    comments = github.list_issue_comments(repo=config.github_repo, issue_number=issue_number)
    reporter_feedback = latest_reporter_correction(comments)
    if not threads and not has_conflict and not reporter_feedback:
        # Stateless no-op: no unresolved feedback, no reporter correction, and the
        # PR merges cleanly.
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
            reporter_feedback=reporter_feedback,
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
            reporter_feedback=reporter_feedback,
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
    reporter_feedback: str = "",
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
    needs_agent = bool(threads) or bool(conflict_files) or bool(reporter_feedback)
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
                reporter_feedback=bool(reporter_feedback),
            ),
        )
    # Install deps once (fresh worktree) so the agent self-verifies and the
    # verify gate below runs against a prepared tree. A failing setup is an
    # environment/baseline problem, not the revise's fault.
    if fix.setup:
        setup_outcomes = run_setup_commands(fix=fix, cwd=plan.agent_cwd)
        if not verify_all_passed(setup_outcomes):
            _post_baseline_broken(
                config=config,
                issue=issue,
                base_branch=pr.base_ref,
                verify_outcomes=setup_outcomes,
                github=github,
                lark=lark,
            )
            return "setup_failed"
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
        worktree_commit_all(
            plan.agent_cwd,
            message=_revise_commit_message(
                issue.number, has_conflict, threads, pr.base_ref, bool(reporter_feedback)
            ),
        )
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
        reporter_feedback=bool(reporter_feedback),
    )
    for thread in threads:
        github.resolve_review_thread(thread_id=thread.id)
    if threads or (reporter_feedback and not has_conflict):
        return "revised"
    return "conflict_resolved"


def _revise_commit_message(
    issue_number: int,
    has_conflict: bool,
    threads: Sequence[ReviewThread],
    base_branch: str,
    reporter_feedback: bool = False,
) -> str:
    if has_conflict and threads:
        return f"fix: merge {base_branch} and address review feedback (#{issue_number})"
    if has_conflict:
        return f"fix: merge {base_branch} to resolve conflicts (#{issue_number})"
    if threads:
        return f"fix: address review feedback (#{issue_number})"
    if reporter_feedback:
        return f"fix: incorporate reporter follow-up (#{issue_number})"
    return f"fix: address review feedback (#{issue_number})"


def _resolve_managed_closing_issue(
    *,
    config: ProjectConfig,
    github: GitHubCliIssuesClient,
    pr: OpenPullRequest,
) -> GitHubIssue | None:
    """The first bugpatrol-managed issue this PR closes, or None.

    A PR reports its CI result to the managed issue it resolves via GitHub's
    native closing-issue link, so both a bugpatrol fix PR and a human PR that
    cites ``Fixes #N`` surface to the reporter's topic by association.
    """
    for number in pr.closing_issue_numbers:
        issue = github.get_issue(repo=config.github_repo, issue_number=number)
        if is_bugpatrol_managed_issue(issue):
            return issue
    return None


def run_ci_feedback(
    *,
    config: ProjectConfig,
    head_branch: str,
    head_sha: str,
    conclusion: str,
    base_repo: Path,
    output_dir: Path,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None = None,
    prompt_path: Path = Path("prompts/fix.zh.md"),
) -> str:
    """React to a project PR CI build finishing, reporting to the managed issue.

    The general PR-CI-feedback path: resolve the open PR for ``head_branch`` and
    the managed issue it closes, then branch on the CI conclusion.
      - success -> build-ready ping to the issue + reporter's Lark topic, for any
        managed-issue PR (a human's manual-fix PR too, not just bugpatrol's);
      - failure on a bugpatrol fix branch -> auto-revise (run_ci_fix);
      - failure on a human branch -> notify the topic only (BugPatrol does not
        revise a branch it does not own).
    Statuses: no_pr, no_managed_issue, ci_failure_notified,
    ci_failure_already_notified, no_ci_failure, plus the underlying run_ci_fix /
    run_build_ready statuses.
    """
    if config.fix is None:
        raise ValueError("project config has no [fix] table; auto-fix is not enabled")
    fix = config.fix
    pr = github.get_open_pull_request_by_head(repo=config.github_repo, head=head_branch)
    if pr is None:
        # No open PR for this branch (merged/closed, or the build ran on a branch
        # push rather than a PR): nothing to report.
        return "no_pr"
    issue = _resolve_managed_closing_issue(config=config, github=github, pr=pr)
    if issue is None:
        # The PR does not close a bugpatrol-managed issue: not ours to report.
        return "no_managed_issue"
    if conclusion == "success":
        return run_build_ready(
            config=config, issue=issue, pr=pr, head_sha=head_sha, github=github, lark=lark
        )
    # failure: auto-revise our own fix branch; only notify a human's branch.
    if head_branch == fix.branch_for_issue(issue.number):
        return run_ci_fix(
            config=config,
            issue=issue,
            pr=pr,
            head_sha=head_sha,
            base_repo=base_repo,
            output_dir=output_dir,
            github=github,
            issue_fields=issue_fields,
            lark=lark,
            prompt_path=prompt_path,
        )
    return _notify_human_pr_ci_failure(
        config=config, issue=issue, pr=pr, head_sha=head_sha, github=github, lark=lark
    )


def _notify_human_pr_ci_failure(
    *,
    config: ProjectConfig,
    issue: GitHubIssue,
    pr: OpenPullRequest,
    head_sha: str,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None = None,
) -> str:
    """Report a failing CI build on a human PR to the issue + Lark topic.

    Notify-only (no auto-revise): de-dupes on ``last_failure_notified_sha`` in
    the PR's CI-fix meta so N failed workflows for one commit notify once.
    """
    comments = github.list_pull_request_comments(repo=config.github_repo, pr_number=pr.number)
    meta = latest_ci_fix_meta(comments)
    if meta.get("last_failure_notified_sha") == head_sha:
        return "ci_failure_already_notified"
    failed_runs = github.list_failed_runs_for_sha(repo=config.github_repo, head_sha=head_sha)
    if not failed_runs:
        # The event fired but no run for this sha concluded failure (re-run green,
        # or a transient/cancelled conclusion): nothing to report.
        return "no_ci_failure"
    assignee = issue.assignees[0] if issue.assignees else ""
    assignee_open_id = (config.lark.user_open_ids or {}).get(assignee, "") if assignee else ""
    notify_pr_ci_failure(
        repo=config.github_repo,
        issue_number=issue.number,
        issue_url=issue.url,
        pr_url=pr.url,
        head_sha=head_sha,
        failed_names=tuple(r.workflow_name or r.name for r in failed_runs),
        meta={**meta, "last_failure_notified_sha": head_sha},
        github=github,
        lark=lark,
        assignee_open_id=assignee_open_id,
    )
    return "ci_failure_notified"


def run_ci_fix(
    *,
    config: ProjectConfig,
    issue: GitHubIssue,
    pr: OpenPullRequest,
    head_sha: str,
    base_repo: Path,
    output_dir: Path,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkMessengerClient | None = None,
    prompt_path: Path = Path("prompts/fix.zh.md"),
) -> str:
    """React to a failed PR CI build on a bugpatrol fix branch (auto-revise).

    Only for branches BugPatrol owns (``bugpatrol/fix-issue-N``); the caller
    (run_ci_feedback) resolves the managed ``issue`` + open ``pr`` and gates this
    on the head branch. Stateless like run_fix_revise: rebuild from origin (the
    fix branch) and read the failed runs + de-dupe meta from GitHub, so any
    runner can react. De-dupe keys on ``head_sha`` (one push → many failed
    workflows → many events), not run_id. Bounded by
    ``[fix.gate].max_ci_fix_attempts``; at the cap it escalates to the PR
    reviewer instead of editing. Statuses: issue_closed, ci_already_handled,
    no_ci_failure, ci_fix_escalated, ci_fixed, blocked, no_changes, no_output,
    setup_failed, verify_failed.
    """
    if config.fix is None:
        raise ValueError("project config has no [fix] table; auto-fix is not enabled")
    fix = config.fix
    if issue.state == "closed":
        # The issue was closed (won't-fix, or handled elsewhere): stop the CI-fix
        # loop even though the fix PR is still open. Reopen the issue to resume.
        # Silent skip -- CI failures fire this per-workflow (many events/push), so
        # a Lark notice here would spam the topic.
        print(f"issue #{issue.number} is closed; skipping CI fix", file=sys.stderr)
        return "issue_closed"

    head_branch = pr.head_ref
    comments = github.list_pull_request_comments(repo=config.github_repo, pr_number=pr.number)
    meta = latest_ci_fix_meta(comments)
    if meta.get("last_fixed_sha") == head_sha:
        # A sibling failed-run event for this same commit already reacted.
        return "ci_already_handled"
    failed_runs = github.list_failed_runs_for_sha(repo=config.github_repo, head_sha=head_sha)
    if not failed_runs:
        # The event fired but no run for this sha concluded failure (e.g. it was
        # re-run green, or a transient/cancelled conclusion): nothing to fix.
        return "no_ci_failure"

    reviewer = issue.assignees[0] if issue.assignees else ""
    reviewer_open_id = (config.lark.user_open_ids or {}).get(reviewer, "") if reviewer else ""
    attempts_so_far = int(meta.get("attempts") or 0)
    if attempts_so_far >= fix.max_ci_fix_attempts:
        # At the cap: do not edit; hand off to the PR reviewer. Record the sha so
        # sibling events for the same commit are de-duped (not re-escalated).
        notify_ci_escalation(
            repo=config.github_repo,
            issue_number=issue.number,
            issue_url=issue.url,
            pr_url=pr.url,
            failed_names=tuple(r.workflow_name or r.name for r in failed_runs),
            cap=fix.max_ci_fix_attempts,
            meta={**meta, "attempts": attempts_so_far, "last_fixed_sha": head_sha},
            github=github,
            lark=lark,
            reviewer_open_id=reviewer_open_id,
        )
        return "ci_fix_escalated"

    failed_logs = tuple(
        (
            run.workflow_name or run.name,
            github.get_run_failed_logs(repo=config.github_repo, run_id=run.run_id),
        )
        for run in failed_runs
    )
    with fix_revise_worktree(base_repo=base_repo, branch=head_branch) as worktree:
        plan = prepare_fix_run(
            config=config,
            issue_number=issue.number,
            worktree_path=worktree,
            output_dir=output_dir,
            github=github,
            issue_fields=issue_fields,
            base_branch="",
            head_branch=head_branch,
            branch_note="",
            prompt_path=prompt_path,
        )
        with plan.context_path.open("a") as handle:
            handle.write("\n\n" + render_ci_fix_feedback_markdown(failed_logs) + "\n")
        return execute_ci_fix(
            config=config,
            issue=issue,
            plan=plan,
            pr=pr,
            head_sha=head_sha,
            attempt=attempts_so_far + 1,
            prior_meta=meta,
            github=github,
            lark=lark,
        )


def execute_ci_fix(
    *,
    config: ProjectConfig,
    issue: GitHubIssue,
    plan: FixRunPlan,
    pr: OpenPullRequest,
    head_sha: str,
    attempt: int,
    prior_meta: dict,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None = None,
) -> str:
    """Run the CI-fix agent, gate the edit, verify, push, and notify.

    A CI fix is an in-scope edit, so the normal diff-size / protected-path gate
    applies (unlike the conflict-merge path). Every terminal path records the sha
    in the BUGPATROL_CI_FIX_META marker so sibling failed-run events for the same
    commit are de-duped rather than re-attempted.
    """
    fix = config.fix
    assert fix is not None  # run_ci_fix guards this.
    if lark is not None:
        send_intake_topic_message(
            repo=config.github_repo,
            issue_number=issue.number,
            github=github,
            lark=lark,
            text=_render_ci_fix_start_message(
                issue_number=issue.number,
                issue_url=issue.url,
                attempt=attempt,
                cap=fix.max_ci_fix_attempts,
            ),
        )
    # Install deps once (fresh worktree) so the CI-fix agent self-verifies and
    # the verify gate runs against a prepared tree. A failing setup is an
    # environment problem; record the sha so sibling events don't re-attempt.
    if fix.setup:
        setup_outcomes = run_setup_commands(fix=fix, cwd=plan.agent_cwd)
        if not verify_all_passed(setup_outcomes):
            _post_baseline_broken(
                config=config,
                issue=issue,
                base_branch=pr.base_ref,
                verify_outcomes=setup_outcomes,
                github=github,
                lark=lark,
            )
            _mark_ci_handled(
                github=github, repo=config.github_repo, pr_url=pr.url,
                prior_meta=prior_meta, attempt=attempt, head_sha=head_sha,
            )
            return "setup_failed"
    run_stats = _run_fix_agent(plan)

    changed_files = worktree_changed_files(plan.agent_cwd)
    diff_line_count = worktree_diff_line_count(plan.agent_cwd)
    gate = evaluate_post_edit(
        changed_files=changed_files, diff_line_count=diff_line_count, fix=fix
    )
    if not gate.allowed:
        _post_blocked(config=config, issue=issue, reason=gate.reason, github=github, lark=lark)
        _mark_ci_handled(
            github=github, repo=config.github_repo, pr_url=pr.url,
            prior_meta=prior_meta, attempt=attempt, head_sha=head_sha,
        )
        return "no_changes" if not changed_files else "blocked"

    if not plan.output_path.exists():
        _post_blocked(
            config=config,
            issue=issue,
            reason="CI-fix agent edited code but wrote no output JSON summary",
            github=github,
            lark=lark,
        )
        _mark_ci_handled(
            github=github, repo=config.github_repo, pr_url=pr.url,
            prior_meta=prior_meta, attempt=attempt, head_sha=head_sha,
        )
        return "no_output"
    result = parse_fix_result(json.loads(plan.output_path.read_text()))

    verify_outcomes = run_verify_commands(fix=fix, cwd=plan.agent_cwd)
    if not verify_all_passed(verify_outcomes):
        _post_verify_failed(
            config=config, issue=issue, verify_outcomes=verify_outcomes, github=github, lark=lark
        )
        _mark_ci_handled(
            github=github, repo=config.github_repo, pr_url=pr.url,
            prior_meta=prior_meta, attempt=attempt, head_sha=head_sha,
        )
        return "verify_failed"

    if changed_files:
        worktree_commit_all(
            plan.agent_cwd, message=f"fix: address CI failure (#{issue.number}) [ci-fix]"
        )
    worktree_push_branch(plan.agent_cwd, branch=plan.head_branch)
    # Notify BEFORE the meta marker lands (Lark-first, marker-last): the PR
    # comment inside notify_ci_fix carries the de-dupe meta.
    notify_ci_fix(
        repo=config.github_repo,
        issue_number=issue.number,
        issue_url=issue.url,
        pr_url=pr.url,
        result=result,
        attempt=attempt,
        cap=fix.max_ci_fix_attempts,
        meta={**prior_meta, "attempts": attempt, "last_fixed_sha": head_sha},
        github=github,
        lark=lark,
        reviewer_open_id=plan.reviewer_open_id,
        run_stats=run_stats,
    )
    return "ci_fixed"


def _mark_ci_handled(
    *,
    github: GitHubCliIssuesClient,
    repo: str,
    pr_url: str,
    prior_meta: dict,
    attempt: int,
    head_sha: str,
) -> None:
    """Record that this commit's CI failure was reacted to (de-dupe marker).

    Written on the non-fixing terminal paths (blocked / no_output / verify_failed)
    where notify_ci_fix does not run, so sibling failed-run events for the same
    sha see last_fixed_sha and skip instead of re-attempting.
    """
    github.add_pull_request_comment(
        repo=repo,
        pr=pr_url,
        body=append_ci_fix_metadata(
            "BugPatrol：本次 CI 失败已处理（详情见 issue 评论）。",
            {**prior_meta, "attempts": attempt, "last_fixed_sha": head_sha},
        ),
    )


def run_build_ready(
    *,
    config: ProjectConfig,
    issue: GitHubIssue,
    pr: OpenPullRequest,
    head_sha: str,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None = None,
) -> str:
    """Surface a passing PR build to the issue + reporter's Lark topic.

    Works for any PR that closes a managed issue -- a bugpatrol fix PR or a
    human's PR -- since the caller (run_ci_feedback) resolves the managed
    ``issue`` + open ``pr`` by association, not by branch name. Pure
    notification (no worktree, no agent, no gate): the PR built cleanly and is
    testable. De-dupes the main "可测试" ping on ``head_sha`` via
    ``last_notified_sha`` so N green build workflows for one commit notify once.
    Install/preview links, though, are posted by the slower builds (iOS/Android)
    minutes AFTER a fast build first trips build-ready, so the first ping may
    carry no link. Once the main ping is sent, later invocations for the same sha
    send a follow-up listing only links that newly appeared (tracked in
    ``notified_link_urls``), so a slow build's install link still reaches the
    reporter without re-notifying. Statuses: build_already_notified,
    build_notified, build_links_notified.
    """
    if config.fix is None:
        raise ValueError("project config has no [fix] table; auto-fix is not enabled")
    fix = config.fix
    comments = github.list_pull_request_comments(repo=config.github_repo, pr_number=pr.number)
    meta = latest_ci_fix_meta(comments)

    assignee = issue.assignees[0] if issue.assignees else ""
    assignee_open_id = (config.lark.user_open_ids or {}).get(assignee, "") if assignee else ""
    links = extract_build_links(comments, fix.build_link_patterns)

    if meta.get("last_notified_sha") == head_sha:
        # Main ping already sent for this commit; only chase links that landed
        # after it (slow builds post their install/preview link later).
        notified_urls = list(meta.get("notified_link_urls") or [])
        seen = set(notified_urls)
        new_links = [(label, url) for label, url in links if url not in seen]
        if not new_links:
            return "build_already_notified"
        notify_build_links_followup(
            repo=config.github_repo,
            issue_number=issue.number,
            issue_url=issue.url,
            pr_url=pr.url,
            head_sha=head_sha,
            meta={**meta, "notified_link_urls": notified_urls + [u for _, u in new_links]},
            github=github,
            lark=lark,
            assignee_open_id=assignee_open_id,
            links=new_links,
        )
        return "build_links_notified"

    notify_build_ready(
        repo=config.github_repo,
        issue_number=issue.number,
        issue_url=issue.url,
        pr_url=pr.url,
        head_sha=head_sha,
        meta={
            **meta,
            "last_notified_sha": head_sha,
            "notified_link_urls": [url for _, url in links],
        },
        github=github,
        lark=lark,
        assignee_open_id=assignee_open_id,
        links=links,
    )
    return "build_notified"


def _render_ci_fix_start_message(
    *, issue_number: int, issue_url: str, attempt: int, cap: int
) -> str:
    text = (
        f"CI 构建失败，开始自动修复（第 {attempt}/{cap} 次），"
        f"GitHub issue [#{issue_number}]({issue_url})"
    )
    runner = triage_runner_name()
    if runner:
        text += f"\n修复执行机：{runner}"
    return text


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
    reporter.start()
    try:
        # Prepare the worktree ONCE (e.g. npm ci) so the agent can self-verify
        # (typecheck/test) with deps present and every verify run below is fast.
        # These files persist across the baseline-attribution reset, so setup
        # never re-runs. A failing setup means the base checkout can't even be
        # built — an environment/baseline problem, not the fix's fault — so it is
        # reported as a broken baseline and no PR is opened.
        if fix.setup:
            reporter.set_phase("准备工作区（安装依赖）")
            setup_outcomes = run_setup_commands(fix=fix, cwd=plan.agent_cwd)
            if not verify_all_passed(setup_outcomes):
                _post_baseline_broken(
                    config=config,
                    issue=issue,
                    base_branch=plan.base_branch,
                    verify_outcomes=setup_outcomes,
                    github=github,
                    lark=lark,
                )
                return "setup_failed"

        # Pre-PR self-heal loop: the agent self-verifies in its own turn (it is
        # told the verify commands and iterates to green). This outer loop is the
        # trust boundary and backstop: it independently re-runs the verify gate,
        # and if the agent's edit still fails it (while the baseline is green) it
        # feeds the failure back and lets the agent try again, up to
        # max_verify_fix_attempts — so a fix-introduced error self-heals BEFORE a
        # PR rather than dead-ending on verify_failed. The gate/no_output paths
        # are still terminal (they don't get more attempts).
        max_attempts = fix.max_verify_fix_attempts
        for attempt in range(1, max_attempts + 1):
            reporter.set_phase(
                "agent 正在编辑代码"
                if attempt == 1
                else f"验证未过，第 {attempt}/{max_attempts} 次自纠"
            )
            # Retries reuse the same worktree/plan; drop any prior summary so a
            # retry that writes nothing is caught by the no_output check below
            # instead of parsing a stale result from an earlier attempt.
            plan.output_path.unlink(missing_ok=True)
            run_stats = _run_fix_agent(plan)

            # The gate trusts the real working-tree diff, never the agent's self-report.
            changed_files = worktree_changed_files(plan.agent_cwd)
            diff_line_count = worktree_diff_line_count(plan.agent_cwd)
            gate = evaluate_post_edit(changed_files=changed_files, diff_line_count=diff_line_count, fix=fix)
            if not gate.allowed:
                _post_blocked(config=config, issue=issue, reason=gate.reason, github=github, lark=lark)
                return "no_changes" if not changed_files else "blocked"

            if not plan.output_path.exists():
                # The agent edited code but never wrote its JSON summary; without it
                # we can't build a trustworthy PR body, so treat like a blocked run.
                _post_blocked(
                    config=config,
                    issue=issue,
                    reason="fix agent edited code but wrote no output JSON summary",
                    github=github,
                    lark=lark,
                )
                return "no_output"
            result = parse_fix_result(json.loads(plan.output_path.read_text()))

            reporter.set_phase("跑验证门（preflight）")
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
            if status == "passed":
                break
            # status == "fix_failed": the fix introduced the failure (baseline is
            # green). The attribution check already reset the worktree to the
            # pristine base, so a retry is a fresh attempt informed by the failure.
            if attempt >= max_attempts:
                _post_verify_failed(
                    config=config,
                    issue=issue,
                    verify_outcomes=verify_outcomes,
                    github=github,
                    lark=lark,
                )
                return "verify_failed"
            failed = tuple(
                (outcome.label, outcome.stderr_tail or outcome.stdout_tail)
                for outcome in verify_outcomes
                if not outcome.ok
            )
            with plan.context_path.open("a") as handle:
                handle.write("\n\n" + render_verify_fix_feedback_markdown(failed) + "\n")

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
    *,
    issue_number: int,
    issue_url: str,
    feedback_count: int,
    base_branch: str = "",
    reporter_feedback: bool = False,
) -> str:
    if base_branch and feedback_count:
        head = f"开始解决与 `{base_branch}` 的冲突并按评审反馈更新修复（{feedback_count} 条）"
    elif base_branch:
        head = f"开始合并目标分支 `{base_branch}` 解决冲突"
    elif feedback_count:
        head = f"开始按评审反馈更新修复（{feedback_count} 条）"
    elif reporter_feedback:
        head = "开始根据上报人的最新反馈更新修复"
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
