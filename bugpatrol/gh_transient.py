"""Shared detection of transient `gh` failures worth a bounded retry.

Single source of truth so the gateway/transport patterns cannot drift between
the two `gh` wrappers (`github.py`'s issue client and `github_fields.py`'s
issue-fields client). Both establish connections to the same GitHub API through
the same Go http client, so both see the same transient blips.

Also hosts the one retry driver both wrappers run, including the proxy→direct
fallback: a proxy that is *up but broken* looks exactly like a network blip to a
single-transport retry, so the second transport is what actually recovers.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

# GitHub occasionally returns a transient gateway/proxy error (e.g. a
# `gh issue edit` that hits a 502 Bad Gateway, or a self-hosted runner proxy
# returning 499 with an empty body). These fail before reaching the backend, so a
# bounded retry is safe and keeps one flaky call from dropping a mutation (like
# the assignee, which is applied last) and failing the whole run.
_TRANSIENT_GATEWAY_RE = re.compile(
    r"non-200 OK status code: 50[234]\b"
    r"|non-200 OK status code: 499\s+body:\s*\"\""
)

# Transport-layer blips from the Go http client `gh` uses. These fail while
# establishing or holding the connection (before or without a completed
# response), so a bounded retry is as safe as the gateway case above — and it
# stops a single flaky call (e.g. `net/http: TLS handshake timeout` on a
# `get_issue_field_values` mid-poll) from crashing the whole watcher.
_TRANSIENT_NETWORK_RE = re.compile(
    r"TLS handshake timeout"
    r"|net/http: request canceled"
    r"|Client\.Timeout exceeded"
    r"|failed to verify certificate: x509: certificate signed by unknown authority"
    r"|(?:dial|read|write) tcp\b"
    r"|i/o timeout"
    r"|connection reset by peer"
    r"|connection refused"
    # Go's http client reports a bare `EOF` (not `unexpected EOF`) when the peer
    # — in CI, the local proxy — closes the connection before any response byte
    # arrives. This crashed a triage run on its very first `get_issue`.
    r"|\bEOF\b"
    r"|server misbehaving"
    r"|no such host",
    re.IGNORECASE,
)


def is_transient_gh_error(stderr: str) -> bool:
    return bool(
        _TRANSIENT_GATEWAY_RE.search(stderr) or _TRANSIENT_NETWORK_RE.search(stderr)
    )


# Every variable that points a Go http client at a proxy (`gh` is Go, and Go's
# ProxyFromEnvironment honours the lowercase and uppercase spellings; ALL_PROXY
# is included because a Go binary run under a curl-style proxy setup inherits it
# too). `NO_PROXY` is deliberately kept — it only ever *removes* hosts from the
# proxied set, so it cannot re-introduce a proxy we just stripped.
_PROXY_ENV_VARS = frozenset({"http_proxy", "https_proxy", "all_proxy"})


def direct_env(env: Mapping[str, str]) -> dict[str, str] | None:
    """`env` with every proxy variable removed, or None if there was none.

    None means "this process is not routed through a proxy", so the caller must
    skip the direct attempt entirely rather than re-run the same command against
    an identical environment — on a CI runner with no proxy configured that
    would be pure added latency for no extra transport.
    """
    stripped = {k: v for k, v in env.items() if k.lower() not in _PROXY_ENV_VARS}
    return None if len(stripped) == len(env) else stripped


class GhRunOutcome(NamedTuple):
    """How the last `gh` attempt ended, and over which transports."""

    completed: subprocess.CompletedProcess[str]
    # A no-proxy transport existed and was tried (or was about to be).
    direct_attempted: bool
    # The result came from the no-proxy transport.
    used_direct: bool

    @property
    def transport_note(self) -> str:
        """Suffix for the caller's error message; '' when there is nothing to say."""
        if self.direct_attempted and self.used_direct:
            return " (and the same command with the proxy stripped also failed)"
        return ""


def run_gh_with_transient_retries(
    command: Sequence[str],
    *,
    stdin: str | None = None,
    retries: int = 1,
    backoff_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    env: Mapping[str, str] = os.environ,
) -> GhRunOutcome:
    """Run `gh`, retrying transient failures, then retrying without the proxy.

    Transports are tried in order: the ambient environment first (the relay's
    watcher plist exports a proxy), then — only when the ambient environment
    actually routes through one — the same command with every proxy variable
    stripped.

    The second transport exists because the proxy can be *reachable but broken*,
    which is invisible to a single-transport retry: on 2026-09-22 the relay's
    mihomo exit node EOFed every `api.github.com` call for 15 hours while the
    direct path succeeded at the same instants. Retrying the proxy alone just
    re-confirms the outage.

    A non-transient failure (401, 404, "Not Found") returns immediately instead
    of burning the fallback: those are answers, not blips. The caller inspects
    the returned process result either way and raises its own error type.
    """
    if retries < 1:
        raise ValueError(f"retries must be at least 1, got {retries}")

    direct = direct_env(env)
    # Env is None (= inherit this process's) for the first transport so `gh`
    # sees exactly what it would have seen before this fallback existed.
    transports: list[tuple[dict[str, str] | None, bool]] = [(None, False)]
    if direct is not None:
        transports.append((direct, True))

    last: subprocess.CompletedProcess[str] | None = None
    for env, used_direct in transports:
        for attempt in range(1, retries + 1):
            completed = subprocess.run(
                command,
                input=stdin,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            if completed.returncode == 0:
                return GhRunOutcome(completed, direct is not None, used_direct)
            last = completed
            stderr = completed.stderr.strip()
            if not is_transient_gh_error(stderr):
                return GhRunOutcome(completed, direct is not None, used_direct)
            if attempt < retries:
                sleep(backoff_seconds * attempt)

    # Non-empty transports x retries >= 1, so the loops above always set `last`;
    # assert rather than let a None escape as an AttributeError in the caller.
    assert last is not None, "gh transports ran but recorded no result"
    return GhRunOutcome(last, direct is not None, transports[-1][1])
