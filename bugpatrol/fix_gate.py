"""Safety gates for the auto-fix runner.

A fix run edits real code and opens a PR, so every gate here is a hard block:
if a check fails the run must not open a PR. Gates are split into a pre-fix gate
(cheap checks before spending an agent turn) and a post-edit gate (evaluated
against the *real* git diff, never the agent's self-report).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bugpatrol.config import FixConfig


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class VerifyOutcome:
    label: str
    command: str
    returncode: int
    stdout_tail: str
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def evaluate_triage_readiness(*, verdict: str, fix: FixConfig) -> GateDecision:
    """Block a fix unless triage reached a verdict that is actually a code fix.

    Verdicts like 信息不足/重复/预期行为/PRD * are not something an auto-fix
    should touch; only `allowed_verdicts` (default 代码 Bug) proceed.
    """
    if not verdict:
        return GateDecision(
            allowed=False,
            reason="issue has no triage verdict yet; run triage before fix",
        )
    if verdict not in fix.allowed_verdicts:
        allowed = "、".join(fix.allowed_verdicts)
        return GateDecision(
            allowed=False,
            reason=f"triage verdict `{verdict}` is not auto-fixable (allowed: {allowed})",
        )
    return GateDecision(allowed=True)


def evaluate_post_edit(
    *,
    changed_files: tuple[str, ...],
    diff_line_count: int,
    fix: FixConfig,
) -> GateDecision:
    """Gate the actual working-tree diff before committing/opening a PR."""
    if not changed_files:
        return GateDecision(
            allowed=False,
            reason="the fix agent made no code changes",
        )
    protected = protected_path_hits(changed_files, fix.protected_globs)
    if protected:
        joined = ", ".join(protected)
        return GateDecision(
            allowed=False,
            reason=f"diff touches protected paths (needs human review): {joined}",
        )
    if diff_line_count > fix.max_diff_lines:
        return GateDecision(
            allowed=False,
            reason=(
                f"diff is too large for auto-fix: {diff_line_count} lines "
                f"> limit {fix.max_diff_lines}; needs human review"
            ),
        )
    return GateDecision(allowed=True)


def protected_path_hits(files: tuple[str, ...], globs: tuple[str, ...]) -> tuple[str, ...]:
    patterns = [_glob_to_regex(pattern) for pattern in globs]
    hits = [path for path in files if any(pattern.match(path) for pattern in patterns)]
    return tuple(hits)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob (supporting `**`) to an anchored regex.

    `**/` matches any number of leading path segments (including none), `**`
    matches across segments, `*`/`?` stay within a segment.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def run_verify_commands(*, fix: FixConfig, cwd: Path) -> tuple[VerifyOutcome, ...]:
    """Run each configured verify command in `cwd`; report every outcome.

    BugPatrol owns none of these commands — they come from the project config —
    so this only shells out and records the exit code and a tail of output for
    the failure summary. All commands run even if an earlier one fails, so the
    report shows every gate at once.
    """
    outcomes: list[VerifyOutcome] = []
    for label, command in fix.verify.items():
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        outcomes.append(
            VerifyOutcome(
                label=label,
                command=command,
                returncode=completed.returncode,
                stdout_tail=_tail(completed.stdout),
                stderr_tail=_tail(completed.stderr),
            )
        )
    return tuple(outcomes)


def verify_all_passed(outcomes: tuple[VerifyOutcome, ...]) -> bool:
    return all(outcome.ok for outcome in outcomes)


def _tail(text: str | None, *, max_lines: int = 30) -> str:
    if not text:
        return ""
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-max_lines:])
