#!/usr/bin/env bash
#
# Bootstrap a runner-owned, per-runner cache clone of a repo for BugPatrol
# triage, without persisting any credential on disk.
#
# Why a persistent cache clone instead of actions/checkout:
#   - Branch resolution needs full history (merge-base --is-ancestor / cat-file
#     -e), which a per-job shallow checkout breaks.
#   - Re-downloading a large repo every run is wasteful.
#   - Triage runs off ephemeral detached worktrees over a shared object DB; a
#     persistent clone is the natural base_repo for that.
#
# Why per-runner ($RUNNER_NAME) namespacing:
#   - One physical machine may host several triage runners. A self-hosted runner
#     is single-concurrency, so a per-runner cache makes every git op (fetch /
#     worktree add / prune) serial and race-free by construction. The only
#     hazard is two runners sharing one cache, which the namespace prevents.
#
# Credentials never touch disk: origin stays a tokenless https URL and auth is
# supplied by a repo-local git credential helper that echoes $GH_TOKEN at call
# time. It is repo-local (never `git config --global`, which is shared per-user
# and would race across concurrent runners) and nothing is written to
# .git/config beyond the helper definition.
#
# Usage:
#   GH_TOKEN=... bugpatrol-cache-bootstrap.sh <owner/repo>
# Optional env:
#   BUGPATROL_CACHE_ROOT  cache root (default: $HOME/.bugpatrol-cache)
#   RUNNER_NAME           runner namespace (default: "default")
# Prints the absolute cache clone path on stdout.

set -euo pipefail

repo="${1:?usage: bugpatrol-cache-bootstrap.sh <owner/repo>}"
: "${GH_TOKEN:?GH_TOKEN must be set}"

cache_root="${BUGPATROL_CACHE_ROOT:-$HOME/.bugpatrol-cache}"
runner_ns="${RUNNER_NAME:-default}"
clone="$cache_root/$runner_ns/$repo"

# Repo-local credential helper: reads $GH_TOKEN from the environment on every
# git call, so no token is persisted. x-access-token is the username GitHub
# expects for token auth.
helper='!f() { echo "username=x-access-token"; echo "password=$GH_TOKEN"; }; f'

# Bounded fetch/clone retry — never loop unbounded on a flaky network. The
# proxy on some runners intermittently drops github.com:443 for minutes at a
# time (frpc tunnels, SSL_ERROR_SYSCALL), and git fails fast on each attempt,
# so retries are cheap. 15 attempts with a capped backoff covers a ~5min blip;
# a genuinely dead network still gives up and reports instead of hanging.
max_attempts=15

# On terminal git failure, print an actionable network diagnostic. A restricted
# network (e.g. a home ISP) can silently drop github's HTTPS *pack* transfer
# while still answering metadata GETs — so `git ls-remote` succeeds but any
# clone/fetch times out. That looks like a dead machine but is usually just a
# missing proxy in the runner env, not an unfixable box. Say so out loud.
git_failure_hint() {
  {
    echo "----"
    echo "BugPatrol cache bootstrap: git HTTPS to github.com/$repo failed."
    echo "http_proxy=${http_proxy:-<unset>} https_proxy=${https_proxy:-<unset>}"
    if [[ -z "${http_proxy:-}${https_proxy:-}${HTTPS_PROXY:-}" ]]; then
      echo "No proxy is set. If this runner is on a restricted/home network that"
      echo "blocks github HTTPS pack transfer (git ls-remote works but clone times"
      echo "out), point the runner .env at a working proxy, e.g. a local Surge:"
      echo "  http_proxy=http://127.0.0.1:6152"
      echo "  https_proxy=http://127.0.0.1:6152"
      echo "then restart the runner. Do NOT assume the machine is unusable."
    fi
    echo "----"
  } >&2
}

run_git_with_retry() {
  local attempt=1
  while true; do
    if git "$@"; then
      return 0
    fi
    if (( attempt >= max_attempts )); then
      echo "git $* failed after $attempt attempts" >&2
      git_failure_hint
      return 1
    fi
    echo "git $* failed (attempt $attempt/$max_attempts); retrying" >&2
    attempt=$(( attempt + 1 ))
    # Backoff grows but caps at 20s so a long blip doesn't mean long sleeps.
    delay=$(( attempt * 2 ))
    if (( delay > 20 )); then
      delay=20
    fi
    sleep "$delay"
  done
}

if [[ -d "$clone/.git" ]]; then
  git -C "$clone" config credential.helper "$helper"
  git -C "$clone" remote set-url origin "https://github.com/$repo.git"
  run_git_with_retry -C "$clone" fetch --prune origin
else
  mkdir -p "$(dirname "$clone")"
  run_git_with_retry -c credential.helper="$helper" \
    clone "https://github.com/$repo.git" "$clone"
  git -C "$clone" config credential.helper "$helper"
fi

echo "$clone"
