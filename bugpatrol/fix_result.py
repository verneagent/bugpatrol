"""Validate and land auto-fix agent results (open PR, notify)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from bugpatrol.clients import GitHubIssueComment, LarkMessengerClient, ReviewThread
from bugpatrol.config import BuildLinkPattern, ProjectConfig
from bugpatrol.fix_gate import VerifyOutcome
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.triage_result import (
    TriageRunStats,
    format_run_stats,
    send_intake_topic_message,
    triage_runner_name,
)

FIX_META_START = "<!-- BUGPATROL_FIX_META"
FIX_META_END = "BUGPATROL_FIX_META -->"
FIX_META_RE = re.compile(
    rf"{re.escape(FIX_META_START)}\s*(.*?)\s*{re.escape(FIX_META_END)}",
    re.DOTALL,
)


def _pr_link(pr_url: str) -> str:
    """Masked PR link for Lark: `[#3996](url)` displays a clickable `#3996`.

    `reply_to_message` renders markdown `[text](url)` as a rich-text link, so we
    never emit a bare full URL. PR urls end in `/pull/<number>`.
    """
    number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    label = f"#{number}" if number.isdigit() else pr_url
    return f"[{label}]({pr_url})"


@dataclass(frozen=True)
class FixResult:
    summary: str
    root_cause: str
    tests_added: bool
    pr_title: str
    pr_body: str


def parse_fix_result(data: dict[str, Any]) -> FixResult:
    return FixResult(
        summary=_required_str(data, "summary"),
        root_cause=_required_str(data, "root_cause"),
        tests_added=_required_bool(data, "tests_added"),
        pr_title=_required_str(data, "pr_title"),
        pr_body=_required_str(data, "pr_body"),
    )


def fix_result_fingerprint(*, issue_number: int, changed_files: tuple[str, ...]) -> str:
    payload = {"issue": issue_number, "changed_files": sorted(changed_files)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_pr_body(
    *,
    result: FixResult,
    issue_number: int,
    issue_url: str,
    changed_files: tuple[str, ...],
    verify_outcomes: tuple[VerifyOutcome, ...],
) -> str:
    lines = [
        result.pr_body.rstrip(),
        "",
        f"Fixes #{issue_number}",
        "",
        "## 根因",
        result.root_cause.strip(),
        "",
        "## 改动",
        result.summary.strip(),
        "",
        "## 改动文件",
    ]
    lines.extend(f"- `{path}`" for path in changed_files)
    lines.extend(["", "## 验证结果"])
    for outcome in verify_outcomes:
        mark = "✅" if outcome.ok else "❌"
        lines.append(f"- {mark} `{outcome.label}`: `{outcome.command}`")
    lines.append("")
    lines.append(f"测试：{'已添加/调整' if result.tests_added else '未新增'}")
    lines.append("")
    lines.append(
        "> 本 PR 由 BugPatrol 自动修复生成，基于上游 triage 的已确认根因。"
        "请人工 review 后再合并——BugPatrol 不会自动合并。"
    )
    runner = triage_runner_name()
    if runner:
        lines.append(f"> 修复执行机：`{runner}`")
    return "\n".join(lines)


def render_fix_comment(*, pr_url: str, result: FixResult, fingerprint: str, issue_number: int) -> str:
    body = "\n".join(
        [
            "## BugPatrol 自动修复",
            "",
            f"已根据 triage 根因生成修复 PR：{pr_url}",
            "",
            f"根因：{result.root_cause.strip()}",
            f"改动：{result.summary.strip()}",
            f"测试：{'已添加/调整' if result.tests_added else '未新增'}",
            "",
            "请负责人 review 后合并（BugPatrol 不会自动合并）。",
        ]
    )
    return append_fix_metadata(
        body,
        {"version": 1, "issue": issue_number, "pr_url": pr_url, "result_fingerprint": fingerprint},
    )


def render_fix_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    result: FixResult,
    reviewer_open_id: str = "",
    run_stats: TriageRunStats | None = None,
) -> str:
    reviewer = f'<at user_id="{reviewer_open_id}"></at> ' if reviewer_open_id else ""
    lines = [
        f"{reviewer}已自动生成修复 PR，GitHub issue [#{issue_number}]({issue_url})",
        f"PR：{_pr_link(pr_url)}",
        f"改动：{result.summary.strip()}",
        "请 review 后合并（不会自动合并）。",
    ]
    runner = triage_runner_name()
    if runner:
        lines.append(f"修复执行机：{runner}")
    stats_line = format_run_stats(run_stats)
    if stats_line:
        lines.append(stats_line)
    return "\n".join(lines)


def render_fix_blocked_lark_message(*, issue_number: int, issue_url: str, reason: str) -> str:
    return "\n".join(
        [
            f"自动修复未通过闸门，GitHub issue [#{issue_number}]({issue_url})",
            reason,
            "已跳过，未开 PR，待人工处理。",
        ]
    )


def render_verify_failed_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    verify_outcomes: tuple[VerifyOutcome, ...],
) -> str:
    lines = [
        f"自动修复未通过验证，GitHub issue [#{issue_number}]({issue_url})",
    ]
    for outcome in verify_outcomes:
        mark = "✅" if outcome.ok else "❌"
        lines.append(f"{mark} {outcome.label}（exit {outcome.returncode}）")
    lines.append("已跳过，未开 PR，待人工处理。")
    return "\n".join(lines)


def render_verify_failed_comment(
    *,
    verify_outcomes: tuple[VerifyOutcome, ...],
) -> str:
    lines = ["## BugPatrol 自动修复未通过验证", "", "修复改动跑验证命令时失败，已放弃开 PR：", ""]
    for outcome in verify_outcomes:
        mark = "✅" if outcome.ok else "❌"
        lines.append(f"- {mark} `{outcome.label}` (`{outcome.command}`, exit {outcome.returncode})")
        tail = outcome.stderr_tail or outcome.stdout_tail
        if not outcome.ok and tail:
            lines.append("")
            lines.append("```")
            lines.append(tail)
            lines.append("```")
    return "\n".join(lines)


def render_blocked_comment(*, reason: str) -> str:
    return "\n".join(["## BugPatrol 自动修复已跳过", "", reason, "", "未开 PR，待人工处理。"])


def render_baseline_broken_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    base_branch: str,
    verify_outcomes: tuple[VerifyOutcome, ...],
) -> str:
    lines = [
        f"⚠️ 目标分支 `{base_branch}` 自身未通过验证（baseline 本就红），"
        f"GitHub issue [#{issue_number}]({issue_url})",
        "把修复回退到未改动的 baseline 后同一验证仍失败，所以这不是本次修复引入的：",
    ]
    for outcome in verify_outcomes:
        mark = "✅" if outcome.ok else "❌"
        lines.append(f"{mark} {outcome.label}（exit {outcome.returncode}）")
    lines.append(f"已暂停修复，未开 PR。请先修复 `{base_branch}` 的 baseline，绿了之后再重跑修复。")
    return "\n".join(lines)


def render_baseline_broken_comment(
    *,
    base_branch: str,
    verify_outcomes: tuple[VerifyOutcome, ...],
) -> str:
    lines = [
        "## BugPatrol 暂停修复：目标分支 baseline 本就红",
        "",
        f"把本次修复回退到未改动的 `{base_branch}` baseline 后，同一验证命令仍然失败，"
        "说明失败不是本次修复引入的，而是目标分支自身已经红了：",
        "",
    ]
    for outcome in verify_outcomes:
        mark = "✅" if outcome.ok else "❌"
        lines.append(f"- {mark} `{outcome.label}` (`{outcome.command}`, exit {outcome.returncode})")
        tail = outcome.stderr_tail or outcome.stdout_tail
        if not outcome.ok and tail:
            lines.append("")
            lines.append("```")
            lines.append(tail)
            lines.append("```")
    lines.append("")
    lines.append(f"已暂停修复、未开 PR。请先让 `{base_branch}` 的 baseline 通过验证，然后重跑修复。")
    return "\n".join(lines)


def notify_fix_pr(
    *,
    repo: str,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    result: FixResult,
    fingerprint: str,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
    reviewer_open_id: str = "",
    run_stats: TriageRunStats | None = None,
) -> None:
    """Post the fix PR link to the issue and the Lark topic (Lark-first).

    Lark-first, marker-last (like triage_result/close_audit): the GitHub comment
    carries the idempotency marker, so send the best-effort Lark ping before it
    to avoid a silently-lost notification if Lark fails after the marker lands.
    """
    if lark is not None:
        send_intake_topic_message(
            repo=repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            text=render_fix_lark_message(
                issue_number=issue_number,
                issue_url=issue_url,
                pr_url=pr_url,
                result=result,
                reviewer_open_id=reviewer_open_id,
                run_stats=run_stats,
            ),
        )
    github.add_issue_comment(
        repo=repo,
        issue_number=issue_number,
        body=render_fix_comment(
            pr_url=pr_url,
            result=result,
            fingerprint=fingerprint,
            issue_number=issue_number,
        ),
    )


def render_review_feedback_markdown(threads: tuple[ReviewThread, ...]) -> str:
    """Render unresolved review threads as instructions for the revise agent."""
    lines = [
        "## PR 评审反馈（需要逐条处理）",
        "",
        "以下是这个 PR 上尚未解决的评审意见。请**只针对这些反馈**做最小改动，"
        "不要重开根因分析、不要扩大范围。",
        "",
    ]
    for index, thread in enumerate(threads, start=1):
        lines.append(f"### 反馈 {index}")
        for comment in thread.comments:
            location = ""
            if comment.path:
                location = f"`{comment.path}"
                if comment.line is not None:
                    location += f":{comment.line}"
                location += "` "
            author = f"@{comment.author} " if comment.author else ""
            lines.append(f"- {location}{author}{comment.body.strip()}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_reporter_feedback_markdown(correction: str) -> str:
    """Render the reporter's latest material follow-up as revise instructions.

    A fix PR is already open, so a fresh material correction from the reporter
    (not an ack) means the fix itself needs to change — possibly reversing an
    earlier wrong direction. Unlike review feedback (which refines the diff),
    this may require re-fixing, so the wording explicitly permits changing the
    prior fix while still holding the scope to this issue.
    """
    return "\n".join(
        [
            "## 上报人补充 / 纠正（据此调整修复）",
            "",
            "这个 issue 的修复 PR 已经打开，但上报人又补充了新信息或指出之前的修复方向不对。"
            "请结合下面这条最新反馈调整当前 PR：可以修改之前的修复（包括推翻错误方向），"
            "但只针对这个 issue，做最小必要改动，不要重开无关的根因分析、不要扩大范围。",
            "",
            correction.strip(),
        ]
    )


def render_conflict_instructions_markdown(*, base_branch: str, files: tuple[str, ...]) -> str:
    """Instruct the revise agent to resolve an in-progress merge's conflicts."""
    lines = [
        f"## 与目标分支 `{base_branch}` 的合并冲突（需要先解决）",
        "",
        f"这个 PR 与目标分支 `{base_branch}` 冲突。已把 `{base_branch}` 合并进当前分支，"
        "以下文件带有冲突标记（`<<<<<<<` / `=======` / `>>>>>>>`），"
        "请在动其它改动之前**先解决这些冲突**：",
        "",
    ]
    lines.extend(f"- `{path}`" for path in files)
    lines.extend(
        [
            "",
            "解决时保留双方各自的意图：既不要丢掉目标分支的新改动，也不要丢掉本修复的改动；"
            "改完请**删除所有冲突标记**。",
        ]
    )
    return "\n".join(lines)


def render_revise_pr_comment(
    *,
    result: FixResult,
    addressed: int,
    conflicted: bool = False,
    base_branch: str = "",
    reporter_feedback: bool = False,
) -> str:
    if conflicted and addressed == 0:
        headline = f"已合并目标分支 `{base_branch}` 解决冲突并推送更新到本 PR。"
    elif conflicted:
        headline = (
            f"已合并目标分支 `{base_branch}` 解决冲突，并处理 {addressed} 条评审意见，"
            "推送更新到本 PR。"
        )
    elif addressed == 0 and reporter_feedback:
        headline = "已根据上报人的最新反馈更新修复并推送到本 PR。"
    else:
        headline = f"已处理 {addressed} 条评审意见并推送更新到本 PR。"
    if addressed:
        tail = "对应的 review threads 已 resolve；请再次 review（BugPatrol 不会自动合并）。"
    else:
        tail = "请再次 review（BugPatrol 不会自动合并）。"
    return "\n".join(
        [
            "## BugPatrol 已更新修复",
            "",
            headline,
            "",
            f"改动：{result.summary.strip()}",
            f"测试：{'已添加/调整' if result.tests_added else '未新增'}",
            "",
            tail,
        ]
    )


def render_revise_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    result: FixResult,
    addressed: int,
    reviewer_open_id: str = "",
    run_stats: TriageRunStats | None = None,
    conflicted: bool = False,
    base_branch: str = "",
    reporter_feedback: bool = False,
) -> str:
    reviewer = f'<at user_id="{reviewer_open_id}"></at> ' if reviewer_open_id else ""
    if conflicted and addressed == 0:
        head = f"{reviewer}已合并目标分支 `{base_branch}` 解决冲突，GitHub issue [#{issue_number}]({issue_url})"
    elif conflicted:
        head = f"{reviewer}已解决与 `{base_branch}` 的冲突并按评审反馈更新修复 PR，GitHub issue [#{issue_number}]({issue_url})"
    elif addressed == 0 and reporter_feedback:
        head = f"{reviewer}已根据上报人的最新反馈更新修复 PR，GitHub issue [#{issue_number}]({issue_url})"
    else:
        head = f"{reviewer}已按评审反馈更新修复 PR，GitHub issue [#{issue_number}]({issue_url})"
    lines = [head, f"PR：{_pr_link(pr_url)}"]
    if addressed:
        lines.append(f"处理反馈：{addressed} 条")
    lines.extend([f"改动：{result.summary.strip()}", "请再次 review（不会自动合并）。"])
    runner = triage_runner_name()
    if runner:
        lines.append(f"修复执行机：{runner}")
    stats_line = format_run_stats(run_stats)
    if stats_line:
        lines.append(stats_line)
    return "\n".join(lines)


def notify_fix_revise(
    *,
    repo: str,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    result: FixResult,
    addressed: int,
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
    reviewer_open_id: str = "",
    run_stats: TriageRunStats | None = None,
    conflicted: bool = False,
    base_branch: str = "",
    reporter_feedback: bool = False,
) -> None:
    """Notify that revise updated the PR (Lark-first, then PR comment).

    Same Lark-first ordering as notify_fix_pr: send the best-effort Lark ping
    before the durable PR comment so a Lark failure never silently drops it.
    """
    if lark is not None:
        send_intake_topic_message(
            repo=repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            text=render_revise_lark_message(
                issue_number=issue_number,
                issue_url=issue_url,
                pr_url=pr_url,
                result=result,
                addressed=addressed,
                reviewer_open_id=reviewer_open_id,
                run_stats=run_stats,
                conflicted=conflicted,
                base_branch=base_branch,
                reporter_feedback=reporter_feedback,
            ),
        )
    github.add_pull_request_comment(
        repo=repo,
        pr=pr_url,
        body=render_revise_pr_comment(
            result=result,
            addressed=addressed,
            conflicted=conflicted,
            base_branch=base_branch,
            reporter_feedback=reporter_feedback,
        ),
    )


def render_conflict_escalation_pr_comment(*, base_branch: str, files: tuple[str, ...]) -> str:
    lines = [
        "## BugPatrol 冲突过于复杂，需人工解决",
        "",
        f"这个 PR 与目标分支 `{base_branch}` 冲突，冲突文件较多，自动解决不安全，已放弃：",
        "",
    ]
    lines.extend(f"- `{path}`" for path in files)
    lines.append("")
    lines.append("请人工 rebase / 解决冲突后再合并（BugPatrol 不会自动合并）。")
    return "\n".join(lines)


def render_conflict_escalation_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    base_branch: str,
    files: tuple[str, ...],
    reviewer_open_id: str = "",
) -> str:
    reviewer = f'<at user_id="{reviewer_open_id}"></at> ' if reviewer_open_id else ""
    lines = [
        f"{reviewer}修复 PR 与目标分支 `{base_branch}` 冲突且过于复杂，"
        f"自动解决不安全，需人工处理，GitHub issue [#{issue_number}]({issue_url})",
        f"PR：{_pr_link(pr_url)}",
        f"冲突文件：{len(files)} 个",
        "请人工 rebase / 解决冲突后再合并（不会自动合并）。",
    ]
    runner = triage_runner_name()
    if runner:
        lines.append(f"修复执行机：{runner}")
    return "\n".join(lines)


def notify_conflict_escalation(
    *,
    repo: str,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    base_branch: str,
    files: tuple[str, ...],
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
    reviewer_open_id: str = "",
) -> None:
    """Escalate an un-auto-resolvable conflict to a human (Lark-first, then PR).

    Same Lark-first ordering as the other fix notifications so a Lark failure
    never silently drops the escalation before the durable PR comment lands.
    """
    if lark is not None:
        send_intake_topic_message(
            repo=repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            text=render_conflict_escalation_lark_message(
                issue_number=issue_number,
                issue_url=issue_url,
                pr_url=pr_url,
                base_branch=base_branch,
                files=files,
                reviewer_open_id=reviewer_open_id,
            ),
        )
    github.add_pull_request_comment(
        repo=repo,
        pr=pr_url,
        body=render_conflict_escalation_pr_comment(base_branch=base_branch, files=files),
    )


# --- CI feedback loop (PR CI failure → CI-fix, success → build-ready) ---------

CI_FIX_META_START = "<!-- BUGPATROL_CI_FIX_META"
CI_FIX_META_END = "BUGPATROL_CI_FIX_META -->"
CI_FIX_META_RE = re.compile(
    rf"{re.escape(CI_FIX_META_START)}\s*(.*?)\s*{re.escape(CI_FIX_META_END)}",
    re.DOTALL,
)


def parse_ci_fix_metadata(comment_body: str) -> dict[str, Any] | None:
    match = CI_FIX_META_RE.search(comment_body)
    if not match:
        return None
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("CI-fix metadata must be a JSON object")
    return data


def append_ci_fix_metadata(comment_markdown: str, metadata: dict[str, Any]) -> str:
    return (
        f"{comment_markdown.rstrip()}\n\n"
        f"{CI_FIX_META_START}\n"
        f"{json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)}\n"
        f"{CI_FIX_META_END}"
    )


def latest_ci_fix_meta(comments: Sequence[GitHubIssueComment]) -> dict[str, Any]:
    """The most recent BUGPATROL_CI_FIX_META across a PR's comments ({} if none).

    De-dupe keys on the fix PR live here: ``attempts`` / ``last_fixed_sha`` for
    the failure branch, ``last_notified_sha`` for the build-ready branch. Reading
    the latest comment lets any runner reconstruct the same state.
    """
    latest: dict[str, Any] = {}
    for comment in comments:
        meta = parse_ci_fix_metadata(comment.body)
        if meta is not None:
            latest = meta
    return latest


def render_ci_fix_feedback_markdown(failed_logs: Sequence[tuple[str, str]]) -> str:
    """Render failed CI runs (name + log tail) as instructions for the agent."""
    lines = [
        "## CI 构建失败反馈（需要修复）",
        "",
        "这个 PR 最新提交触发的项目 CI 里，以下构建失败了。请**只针对这些失败**做最小修复，"
        "不要重开根因分析、不要扩大范围。日志只保留了尾部错误区域：",
        "",
    ]
    for index, (name, log_tail) in enumerate(failed_logs, start=1):
        lines.append(f"### 失败 {index}：{name}")
        lines.append("")
        lines.append("```")
        lines.append((log_tail or "").strip() or "（无日志）")
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_verify_fix_feedback_markdown(failed: Sequence[tuple[str, str]]) -> str:
    """Render failed pre-PR verify commands (label + log tail) for the agent.

    Fed back INSIDE a single fix run when the agent's own edit fails the verify
    gate (preflight) while the baseline is green. The worktree has been reset to
    the pristine base, so the next attempt starts fresh — the agent must produce
    a DIFFERENT fix that does not reintroduce these failures.
    """
    lines = [
        "## 验证门（preflight）未通过——上一次修复被丢弃，请重做",
        "",
        "你上一次的改动没能通过项目的验证门（下列命令失败）。工作区已回滚到干净的基线，"
        "请**重新**基于原始代码做最小修复，产出一个**不同**的、不会再触发这些错误的方案，"
        "不要重复上一次的错误、不要扩大范围。日志只保留了尾部错误区域：",
        "",
    ]
    for index, (label, log_tail) in enumerate(failed, start=1):
        lines.append(f"### 失败 {index}：{label}")
        lines.append("")
        lines.append("```")
        lines.append((log_tail or "").strip() or "（无日志）")
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_ci_fix_pr_comment(*, result: FixResult, attempt: int, cap: int) -> str:
    return "\n".join(
        [
            "## BugPatrol 已修复 CI 构建失败",
            "",
            f"已根据 CI 失败日志修复并推送更新到本 PR（第 {attempt}/{cap} 次自动修复）。",
            "",
            f"改动：{result.summary.strip()}",
            f"测试：{'已添加/调整' if result.tests_added else '未新增'}",
            "",
            "CI 会在新提交上重跑；仍失败会继续自动修复，达到上限后转人工。",
        ]
    )


def render_ci_fix_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    result: FixResult,
    attempt: int,
    cap: int,
    reviewer_open_id: str = "",
    run_stats: TriageRunStats | None = None,
) -> str:
    reviewer = f'<at user_id="{reviewer_open_id}"></at> ' if reviewer_open_id else ""
    lines = [
        f"{reviewer}已根据 CI 失败日志修复并更新 PR（第 {attempt}/{cap} 次），"
        f"GitHub issue [#{issue_number}]({issue_url})",
        f"PR：{_pr_link(pr_url)}",
        f"改动：{result.summary.strip()}",
        "CI 会重跑；仍失败会继续自动修复，达到上限转人工。",
    ]
    runner = triage_runner_name()
    if runner:
        lines.append(f"修复执行机：{runner}")
    stats_line = format_run_stats(run_stats)
    if stats_line:
        lines.append(stats_line)
    return "\n".join(lines)


def notify_ci_fix(
    *,
    repo: str,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    result: FixResult,
    attempt: int,
    cap: int,
    meta: dict[str, Any],
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
    reviewer_open_id: str = "",
    run_stats: TriageRunStats | None = None,
) -> None:
    """Notify that the CI-fix loop updated the PR (Lark-first, then PR comment).

    The PR comment carries the BUGPATROL_CI_FIX_META marker (attempts +
    last_fixed_sha), so it is written last: a Lark failure must not silently drop
    the ping before the durable de-dupe marker lands (at-least-once).
    """
    if lark is not None:
        send_intake_topic_message(
            repo=repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            text=render_ci_fix_lark_message(
                issue_number=issue_number,
                issue_url=issue_url,
                pr_url=pr_url,
                result=result,
                attempt=attempt,
                cap=cap,
                reviewer_open_id=reviewer_open_id,
                run_stats=run_stats,
            ),
        )
    github.add_pull_request_comment(
        repo=repo,
        pr=pr_url,
        body=append_ci_fix_metadata(
            render_ci_fix_pr_comment(result=result, attempt=attempt, cap=cap), meta
        ),
    )


def render_ci_escalation_pr_comment(*, failed_names: tuple[str, ...], cap: int) -> str:
    lines = [
        "## BugPatrol CI 自动修复达到上限，需人工处理",
        "",
        f"已连续自动修复 {cap} 次，CI 构建仍失败，继续自动修复不安全，已停止：",
        "",
    ]
    lines.extend(f"- `{name}`" for name in failed_names)
    lines.append("")
    lines.append("请人工查看 CI 失败原因后修复（BugPatrol 不会自动合并）。")
    return "\n".join(lines)


def render_ci_escalation_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    failed_names: tuple[str, ...],
    cap: int,
    reviewer_open_id: str = "",
) -> str:
    reviewer = f'<at user_id="{reviewer_open_id}"></at> ' if reviewer_open_id else ""
    lines = [
        f"{reviewer}修复 PR 的 CI 已连续自动修复 {cap} 次仍失败，需人工处理，"
        f"GitHub issue [#{issue_number}]({issue_url})",
        f"PR：{_pr_link(pr_url)}",
        f"失败构建：{len(failed_names)} 个",
        "请人工查看 CI 失败原因后修复（不会自动合并）。",
    ]
    runner = triage_runner_name()
    if runner:
        lines.append(f"修复执行机：{runner}")
    return "\n".join(lines)


def notify_ci_escalation(
    *,
    repo: str,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    failed_names: tuple[str, ...],
    cap: int,
    meta: dict[str, Any],
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
    reviewer_open_id: str = "",
) -> None:
    """Escalate a CI failure that hit the retry cap (Lark-first, then PR comment).

    The PR comment carries the meta marker (last_fixed_sha) so repeated failure
    events for the same commit are de-duped; it is written last.
    """
    if lark is not None:
        send_intake_topic_message(
            repo=repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            text=render_ci_escalation_lark_message(
                issue_number=issue_number,
                issue_url=issue_url,
                pr_url=pr_url,
                failed_names=failed_names,
                cap=cap,
                reviewer_open_id=reviewer_open_id,
            ),
        )
    github.add_pull_request_comment(
        repo=repo,
        pr=pr_url,
        body=append_ci_fix_metadata(
            render_ci_escalation_pr_comment(failed_names=failed_names, cap=cap), meta
        ),
    )


def extract_build_links(
    comments: Sequence[GitHubIssueComment],
    patterns: Sequence[BuildLinkPattern],
) -> tuple[tuple[str, str], ...]:
    """Harvest install/preview links the project's CI posted on the fix PR.

    Each configured pattern's single capture group is the URL. We scan every PR
    comment body; the first match per pattern wins (CI posts one link comment per
    artifact). Deduped by URL so a re-posted comment does not double-list. Returns
    ``(label, url)`` pairs in the patterns' declared order.
    """
    links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for pattern in patterns:
        compiled = re.compile(pattern.pattern)
        for comment in comments:
            match = compiled.search(comment.body)
            if match is None:
                continue
            url = match.group(1)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            links.append((pattern.label, url))
            break
    return tuple(links)


def render_build_ready_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    assignee_open_id: str = "",
    links: Sequence[tuple[str, str]] = (),
) -> str:
    assignee = f'<at user_id="{assignee_open_id}"></at> ' if assignee_open_id else ""
    lines = [
        f"{assignee}✅ 修复构建通过，可测试，GitHub issue [#{issue_number}]({issue_url})",
        f"PR：{_pr_link(pr_url)}",
    ]
    if links:
        lines.extend(f"{label}：[{label}]({url})" for label, url in links)
    runner = triage_runner_name()
    if runner:
        lines.append(f"执行机：{runner}")
    return "\n".join(lines)


def render_build_ready_issue_comment(
    *, pr_url: str, links: Sequence[tuple[str, str]] = ()
) -> str:
    lines = [
        "## BugPatrol 修复构建通过，可测试",
        "",
        f"修复 PR 的 CI 构建已通过，可以测试：{pr_url}",
    ]
    if links:
        lines.append("")
        lines.extend(f"- {label}：{url}" for label, url in links)
    return "\n".join(lines)


def render_build_ready_marker_comment(*, head_sha: str) -> str:
    return f"BugPatrol：构建 `{head_sha[:12]}` 已通知可测试。"


def notify_build_ready(
    *,
    repo: str,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    head_sha: str,
    meta: dict[str, Any],
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
    assignee_open_id: str = "",
    links: Sequence[tuple[str, str]] = (),
) -> None:
    """Surface a passing fix-PR build to the issue + Lark topic (Lark-first).

    Order is Lark → issue comment (human) → PR meta comment (the durable
    last_notified_sha marker, written last) so a Lark failure never silently
    drops the ping before the de-dupe marker lands (at-least-once).
    """
    if lark is not None:
        send_intake_topic_message(
            repo=repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            text=render_build_ready_lark_message(
                issue_number=issue_number,
                issue_url=issue_url,
                pr_url=pr_url,
                assignee_open_id=assignee_open_id,
                links=links,
            ),
        )
    github.add_issue_comment(
        repo=repo,
        issue_number=issue_number,
        body=render_build_ready_issue_comment(pr_url=pr_url, links=links),
    )
    github.add_pull_request_comment(
        repo=repo,
        pr=pr_url,
        body=append_ci_fix_metadata(
            render_build_ready_marker_comment(head_sha=head_sha), meta
        ),
    )


def render_build_links_followup_lark_message(
    *,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    assignee_open_id: str = "",
    links: Sequence[tuple[str, str]],
) -> str:
    assignee = f'<at user_id="{assignee_open_id}"></at> ' if assignee_open_id else ""
    lines = [
        f"{assignee}🔗 修复 #{issue_number} 的安装 / 预览链接已就绪，"
        f"GitHub issue [#{issue_number}]({issue_url})",
        f"PR：{_pr_link(pr_url)}",
    ]
    lines.extend(f"{label}：[{label}]({url})" for label, url in links)
    runner = triage_runner_name()
    if runner:
        lines.append(f"执行机：{runner}")
    return "\n".join(lines)


def render_build_links_followup_issue_comment(
    *, pr_url: str, links: Sequence[tuple[str, str]]
) -> str:
    lines = [
        "## BugPatrol 安装 / 预览链接已就绪",
        "",
        f"修复 PR：{pr_url}",
        "",
    ]
    lines.extend(f"- {label}：{url}" for label, url in links)
    return "\n".join(lines)


def render_build_links_followup_marker_comment(*, head_sha: str) -> str:
    return f"BugPatrol：构建 `{head_sha[:12]}` 的安装 / 预览链接已通知。"


def notify_build_links_followup(
    *,
    repo: str,
    issue_number: int,
    issue_url: str,
    pr_url: str,
    head_sha: str,
    meta: dict[str, Any],
    github: GitHubCliIssuesClient,
    lark: LarkMessengerClient | None,
    assignee_open_id: str = "",
    links: Sequence[tuple[str, str]],
) -> None:
    """Follow-up ping when install/preview links land after the build-ready ping.

    A fast build trips build-ready first; a slow build (iOS/Android) posts its
    install link minutes later. This surfaces only the newly-arrived ``links``
    (never re-listing ones already sent) so the reporter gets the real link
    without spam. Same Lark-first → issue comment → PR meta marker ordering as
    notify_build_ready; the marker carries the updated notified_link_urls set.
    """
    if lark is not None:
        send_intake_topic_message(
            repo=repo,
            issue_number=issue_number,
            github=github,
            lark=lark,
            text=render_build_links_followup_lark_message(
                issue_number=issue_number,
                issue_url=issue_url,
                pr_url=pr_url,
                assignee_open_id=assignee_open_id,
                links=links,
            ),
        )
    github.add_issue_comment(
        repo=repo,
        issue_number=issue_number,
        body=render_build_links_followup_issue_comment(pr_url=pr_url, links=links),
    )
    github.add_pull_request_comment(
        repo=repo,
        pr=pr_url,
        body=append_ci_fix_metadata(
            render_build_links_followup_marker_comment(head_sha=head_sha), meta
        ),
    )


def parse_fix_metadata(comment_body: str) -> dict[str, Any] | None:
    match = FIX_META_RE.search(comment_body)
    if not match:
        return None
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("fix metadata must be a JSON object")
    return data


def append_fix_metadata(comment_markdown: str, metadata: dict[str, Any]) -> str:
    return (
        f"{comment_markdown.rstrip()}\n\n"
        f"{FIX_META_START}\n"
        f"{json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)}\n"
        f"{FIX_META_END}"
    )


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing string field: {key}")
    return value


def _required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"missing boolean field: {key}")
    return value
