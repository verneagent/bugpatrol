"""Tests for the shared `gh` retry driver and its proxy→direct fallback."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from bugpatrol.gh_transient import (
    direct_env,
    is_transient_gh_error,
    run_gh_with_transient_retries,
)

PROXIED = {
    "PATH": "/usr/bin",
    "HTTPS_PROXY": "http://127.0.0.1:18443",
    "https_proxy": "http://127.0.0.1:18443",
    "all_proxy": "socks5://127.0.0.1:18443",
    "NO_PROXY": "localhost,127.0.0.1",
}
UNPROXIED = {"PATH": "/usr/bin"}

TLS = "net/http: TLS handshake timeout"


def _result(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


class DirectEnvTest(unittest.TestCase):
    def test_strips_every_proxy_spelling_and_keeps_no_proxy(self) -> None:
        stripped = direct_env(PROXIED)

        self.assertIsNotNone(stripped)
        assert stripped is not None
        self.assertEqual(stripped, {"PATH": "/usr/bin", "NO_PROXY": "localhost,127.0.0.1"})

    def test_returns_none_when_the_process_is_not_proxied(self) -> None:
        # None is the signal to skip the second transport entirely — there is no
        # no-proxy environment to fall back to.
        self.assertIsNone(direct_env(UNPROXIED))


class ProxyFallbackTest(unittest.TestCase):
    def test_falls_back_to_a_proxy_free_environment(self) -> None:
        # The relay's shape (2026-09-22): the proxy EOFed every call while the
        # direct path worked at the same instants. Retrying the proxy alone just
        # re-confirms the outage.
        with patch("subprocess.run") as run:
            run.side_effect = [
                _result(1, stderr='Post "https://api.github.com/graphql": EOF'),
                _result(1, stderr='Post "https://api.github.com/graphql": EOF'),
                _result(0, stdout="ok"),
            ]
            outcome = run_gh_with_transient_retries(
                ["gh", "issue", "list"],
                retries=2,
                sleep=lambda _s: None,
                env=PROXIED,
            )

        self.assertEqual(outcome.completed.stdout, "ok")
        self.assertTrue(outcome.used_direct)
        self.assertEqual(run.call_count, 3)
        # First pass inherits this process's env (proxy and all)…
        self.assertIsNone(run.call_args_list[0].kwargs["env"])
        self.assertIsNone(run.call_args_list[1].kwargs["env"])
        # …the second pass runs with every proxy variable stripped.
        direct = run.call_args_list[2].kwargs["env"]
        self.assertEqual(direct, {"PATH": "/usr/bin", "NO_PROXY": "localhost,127.0.0.1"})

    def test_retries_without_the_proxy_when_there_was_only_one_retry(self) -> None:
        # A single-attempt caller (resources' non-network path) still gets the
        # second transport.
        with patch("subprocess.run") as run:
            run.side_effect = [
                _result(1, stderr="dial tcp 140.82.112.5:443: i/o timeout"),
                _result(0, stdout="ok"),
            ]
            outcome = run_gh_with_transient_retries(
                ["gh", "api", "/rate_limit"],
                retries=1,
                sleep=lambda _s: None,
                env=PROXIED,
            )

        self.assertEqual(run.call_count, 2)
        self.assertTrue(outcome.used_direct)
        self.assertNotIn("https_proxy", run.call_args_list[1].kwargs["env"])

    def test_does_not_burn_the_fallback_on_a_non_transient_error(self) -> None:
        # A 401/404 is an answer, not a blip: retrying it just delays an
        # actionable error.
        with patch("subprocess.run") as run:
            run.return_value = _result(1, stderr="HTTP 401: Bad credentials")
            outcome = run_gh_with_transient_retries(
                ["gh", "issue", "list"],
                retries=3,
                sleep=lambda _s: None,
                env=PROXIED,
            )

        run.assert_called_once()
        self.assertFalse(outcome.used_direct)
        self.assertTrue(outcome.direct_attempted)

    def test_no_second_transport_when_the_environment_has_no_proxy(self) -> None:
        # CI runners export no proxy: the fallback would re-run the identical
        # command against an identical environment, which is pure latency.
        with patch("subprocess.run") as run:
            run.return_value = _result(1, stderr=TLS)
            outcome = run_gh_with_transient_retries(
                ["gh", "issue", "list"],
                retries=3,
                sleep=lambda _s: None,
                env=UNPROXIED,
            )

        self.assertEqual(run.call_count, 3)
        self.assertFalse(outcome.direct_attempted)
        self.assertFalse(outcome.used_direct)
        self.assertEqual(outcome.transport_note, "")

    def test_reports_both_transports_in_the_note(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = _result(1, stderr=TLS)
            outcome = run_gh_with_transient_retries(
                ["gh", "issue", "list"],
                retries=1,
                sleep=lambda _s: None,
                env=PROXIED,
            )

        self.assertEqual(run.call_count, 2)
        self.assertTrue(outcome.used_direct)
        self.assertEqual(
            outcome.transport_note,
            " (and the same command with the proxy stripped also failed)",
        )

    def test_success_on_the_first_attempt_keeps_the_ambient_environment(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = _result(0, stdout="[]")
            outcome = run_gh_with_transient_retries(
                ["gh", "issue", "list"],
                retries=3,
                sleep=lambda _s: None,
                env=PROXIED,
            )

        run.assert_called_once()
        self.assertIsNone(run.call_args_list[0].kwargs["env"])
        self.assertFalse(outcome.used_direct)
        self.assertEqual(outcome.transport_note, "")

    def test_retries_must_be_at_least_one(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 1"):
            run_gh_with_transient_retries(["gh"], retries=0, env=UNPROXIED)


class TransientClassifierTest(unittest.TestCase):
    def test_classifies_proxy_eof_as_transient(self) -> None:
        # The relay's proxy closed the connection before any response byte.
        self.assertTrue(is_transient_gh_error('Post "https://api.github.com/graphql": EOF'))

    def test_does_not_classify_a_bad_credential_as_transient(self) -> None:
        self.assertFalse(is_transient_gh_error("HTTP 401: Bad credentials"))


if __name__ == "__main__":
    unittest.main()
