from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bugpatrol.config import DEFAULT_FIX_PROTECTED_GLOBS, FixConfig
from bugpatrol.fix_gate import (
    evaluate_post_edit,
    evaluate_triage_readiness,
    protected_path_hits,
    run_verify_commands,
    strip_ansi,
    verify_all_passed,
)


def _fix(**overrides: object) -> FixConfig:
    base = dict(verify={"test": "true"})
    base.update(overrides)
    return FixConfig(**base)  # type: ignore[arg-type]


class TriageReadinessTest(unittest.TestCase):
    def test_blocks_when_no_verdict(self) -> None:
        decision = evaluate_triage_readiness(verdict="", fix=_fix())
        self.assertFalse(decision.allowed)
        self.assertIn("no triage verdict", decision.reason)

    def test_blocks_non_code_bug_verdict(self) -> None:
        decision = evaluate_triage_readiness(verdict="信息不足", fix=_fix())
        self.assertFalse(decision.allowed)
        self.assertIn("not auto-fixable", decision.reason)

    def test_allows_code_bug(self) -> None:
        self.assertTrue(evaluate_triage_readiness(verdict="代码 Bug", fix=_fix()).allowed)

    def test_respects_configured_allowed_verdicts(self) -> None:
        fix = _fix(allowed_verdicts=("代码 Bug", "Case 错误"))
        self.assertTrue(evaluate_triage_readiness(verdict="Case 错误", fix=fix).allowed)


class PostEditGateTest(unittest.TestCase):
    def test_blocks_empty_diff(self) -> None:
        decision = evaluate_post_edit(changed_files=(), diff_line_count=0, fix=_fix())
        self.assertFalse(decision.allowed)
        self.assertIn("no code changes", decision.reason)

    def test_blocks_oversized_diff(self) -> None:
        decision = evaluate_post_edit(
            changed_files=("src/a.ts",),
            diff_line_count=1200,
            fix=_fix(max_diff_lines=800),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("too large", decision.reason)

    def test_blocks_protected_path(self) -> None:
        decision = evaluate_post_edit(
            changed_files=("src/a.ts", ".github/workflows/ci.yml"),
            diff_line_count=10,
            fix=_fix(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn(".github/workflows/ci.yml", decision.reason)

    def test_allows_small_safe_diff(self) -> None:
        decision = evaluate_post_edit(
            changed_files=("src/a.ts", "tests/a.test.ts"),
            diff_line_count=40,
            fix=_fix(),
        )
        self.assertTrue(decision.allowed)


class ProtectedGlobTest(unittest.TestCase):
    def test_default_globs_catch_lockfiles_and_secrets(self) -> None:
        files = (
            "src/index.ts",
            "pnpm-lock.yaml",
            "packages/app/package-lock.json",
            "config/prod.pem",
            "app/.env.local",
            "db/migrations/001_init.sql",
        )
        hits = protected_path_hits(files, DEFAULT_FIX_PROTECTED_GLOBS)
        self.assertNotIn("src/index.ts", hits)
        self.assertIn("pnpm-lock.yaml", hits)
        self.assertIn("packages/app/package-lock.json", hits)
        self.assertIn("config/prod.pem", hits)
        self.assertIn("app/.env.local", hits)
        self.assertIn("db/migrations/001_init.sql", hits)

    def test_nested_github_dir_is_protected(self) -> None:
        hits = protected_path_hits((".github/workflows/x.yml",), DEFAULT_FIX_PROTECTED_GLOBS)
        self.assertEqual(hits, (".github/workflows/x.yml",))


class VerifyCommandsTest(unittest.TestCase):
    def test_runs_all_commands_and_reports_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = _fix(verify={"ok": "true", "fail": "false"})
            outcomes = run_verify_commands(fix=fix, cwd=Path(tmp))
        labels = {o.label: o for o in outcomes}
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(labels["ok"].ok)
        self.assertFalse(labels["fail"].ok)
        self.assertFalse(verify_all_passed(outcomes))

    def test_all_passed_when_every_command_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = run_verify_commands(fix=_fix(verify={"a": "true", "b": "true"}), cwd=Path(tmp))
        self.assertTrue(verify_all_passed(outcomes))

    def test_captures_output_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = run_verify_commands(
                fix=_fix(verify={"echo": "echo hello-tail"}),
                cwd=Path(tmp),
            )
        self.assertIn("hello-tail", outcomes[0].stdout_tail)

    def test_captured_tail_strips_ansi_color_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = run_verify_commands(
                fix=_fix(verify={"colored": "printf '\\033[0;31mLint failed\\033[0m\\n'"}),
                cwd=Path(tmp),
            )
        tail = outcomes[0].stdout_tail
        self.assertIn("Lint failed", tail)
        self.assertNotIn("\x1b", tail)
        self.assertNotIn("[0;31m", tail)


class StripAnsiTest(unittest.TestCase):
    def test_removes_sgr_color_sequences(self) -> None:
        raw = "\x1b[0;31m\x1b[1m'serve' is not installed globally.\x1b[0m"
        self.assertEqual(strip_ansi(raw), "'serve' is not installed globally.")

    def test_removes_cursor_and_erase_sequences(self) -> None:
        raw = "before\x1b[2K\x1b[1Gafter"
        self.assertEqual(strip_ansi(raw), "beforeafter")

    def test_leaves_plain_text_untouched(self) -> None:
        self.assertEqual(strip_ansi("just plain text"), "just plain text")


if __name__ == "__main__":
    unittest.main()
