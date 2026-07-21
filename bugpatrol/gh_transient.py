"""Shared detection of transient `gh` failures worth a bounded retry.

Single source of truth so the gateway/transport patterns cannot drift between
the two `gh` wrappers (`github.py`'s issue client and `github_fields.py`'s
issue-fields client). Both establish connections to the same GitHub API through
the same Go http client, so both see the same transient blips.
"""

from __future__ import annotations

import re

# GitHub occasionally returns a transient gateway error (e.g. a `gh issue edit`
# that hits a 502 Bad Gateway). These fail before reaching the backend, so a
# bounded retry is safe and keeps one flaky call from dropping a mutation (like
# the assignee, which is applied last) and failing the whole run.
_TRANSIENT_GATEWAY_RE = re.compile(r"non-200 OK status code: 50[234]\b")

# Transport-layer blips from the Go http client `gh` uses. These fail while
# establishing or holding the connection (before or without a completed
# response), so a bounded retry is as safe as the gateway case above — and it
# stops a single flaky call (e.g. `net/http: TLS handshake timeout` on a
# `get_issue_field_values` mid-poll) from crashing the whole watcher.
_TRANSIENT_NETWORK_RE = re.compile(
    r"TLS handshake timeout"
    r"|net/http: request canceled"
    r"|Client\.Timeout exceeded"
    r"|(?:dial|read|write) tcp\b"
    r"|i/o timeout"
    r"|connection reset by peer"
    r"|connection refused"
    r"|unexpected EOF"
    r"|server misbehaving"
    r"|no such host",
    re.IGNORECASE,
)


def is_transient_gh_error(stderr: str) -> bool:
    return bool(
        _TRANSIENT_GATEWAY_RE.search(stderr) or _TRANSIENT_NETWORK_RE.search(stderr)
    )
