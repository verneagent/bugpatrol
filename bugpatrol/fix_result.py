"""Validate and land auto-fix agent results (open PR, notify)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from bugpatrol.clients import LarkMessengerClient
from bugpatrol.config import ProjectConfig
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
        f"PR：{pr_url}",
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
