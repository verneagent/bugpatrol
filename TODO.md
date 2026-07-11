# bugpatrol TODO

Project-neutral implementation items.

## Status (2026-07-11)

- Reconcile, cross-repo triage references, and operations tracks: **done**,
  deployed to fived and verified live (reconcile dry-run run 29112422738;
  cross-repo triage #3946 run 29112882288).
- Runner checkout & credential hardening: code/template side **done**; the
  fived production rip-replace (secret migration + deleting the on-disk bot
  key) is **deferred to a maintenance window** — see that section's note.

## Reconcile (done)

- Extend fix notification reconcile beyond JSON input.
  - Collect recent merged PRs, linked commits, closed issues, and issue timeline
    fix events from GitHub.
  - Resolve each candidate to BugPatrol-managed issues only.
  - Skip candidates already covered by `BUGPATROL_FIX_META`.
- Add project-neutral GitHub Actions template:
  `examples/github-actions/bugpatrol-reconcile.yml`.
  - Scheduled trigger.
  - Manual trigger.
  - Separate triage and notify reconcile jobs or clearly separated steps.

## Cross-repo triage references (done)

Goal: an app repo's bug sometimes lives in a sibling repo (e.g. fived's frontend
calls weaver's backend). Let triage read declared reference repos read-only,
without inferring which repo. Must not depend on a runner's pre-existing local
checkout.

- Config: `[[triage.reference_repos]]` with `repo`, `path`, `purpose`, and an
  optional `branch_map` (main-repo branch -> reference-repo branch).
- Branch correlation: default the reference branch to the main repo's resolved
  `target_branch` (same name); `branch_map` overrides only on name mismatch.
- Reference resolution is a *thinner* variant of `resolve_triage_branch`
  (`worktree.resolve_reference_branch`): mapped branch exists on the ref remote
  -> use it; else quietly fall back to `origin/main`. No `tip`/`merged`/
  `deleted_fallback`, no `needs_info` — a missing branch just means this repo
  doesn't participate, it must not pollute the main issue's needs-info.
- Isolation: one ephemeral detached `triage_worktree` per reference repo at the
  resolved ref; manage nested worktree lifetimes with `contextlib.ExitStack`.
  Feed each worktree path into `workspace_dirs` so `--add-dir` admits it; agent
  `cwd` stays the main repo's worktree.
- Context: inject a `## Reference Repos` block (path + purpose + the branch
  actually analyzed) so the agent knows where to cross-check.
- Guard: a declared ref path that failed to materialize must fail loud, not
  silently degrade. Reuse `detect_sandbox_denial` for `--add-dir` scope misses.
- Touches: config.py (`ReferenceRepo`), worktree.py (`resolve_reference_branch`),
  triage_runner/__main__ (nested worktrees + workspace_dirs + context), workflow
  template (ref checkout), plus unit tests.

## Runner checkout & credential hardening (code done; fived rollout DEFERRED)

Goal: keep secrets off the runner disk and stop piggybacking triage on human dev
checkouts. Applies to the example workflows and the deployed ones alike.

> **Deferred (2026-07-11):** the code/template side is done. The fived
> production rip-replace is a high-risk maintenance-window task and is NOT yet
> done. Note: fived already has SOBIT App-token secrets (used by
> `notify-fix.yml` via `actions/create-github-app-token@v3`), so the triage.yml
> migration off `gh-bot-token.sh` + on-disk key `~/.fived-bot/private-key.pem`
> is feasible but must be done under a maintenance window across all 3 runners.

- Credentials from the workflow, not the box: inject `DEEPSEEK_API_KEY` and the
  Lark app secret via Actions `secrets` in the step env; the code already reads
  both from `os.environ`, so no code change. Mint the bot token in-workflow with
  `actions/create-github-app-token` (App id/key as secrets) — no on-disk private
  key, no `gh-bot-token.sh` fallback. Remove the migrated vars from runner `.env`
  afterward; keep only machine-specific non-secrets (proxy vars, runner name).
- Runner-owned cache clone, not a dev checkout: bootstrap a clone-or-fetch of the
  app repo (and reference repos, and the public tool) under a runner-owned cache
  root, decoupled from any `~/clover` dev tree. Self-heals on a fresh runner.
- Per-runner isolation (one physical machine may host several triage runners —
  e.g. two already share one mini): namespace the cache root by `$RUNNER_NAME`
  (`$HOME/.bugpatrol-cache/$RUNNER_NAME`) so two runners never share a clone. A
  self-hosted runner is single-concurrency, so a per-runner cache makes all git
  ops (fetch / worktree add / prune / merge) serial and race-free by
  construction; the only hazard is two runners sharing one cache. Write the
  credential helper as *repo-local* config in each cache (never `git config
  --global`, which is shared per-user) so concurrent runners don't race global
  git state and no token persists across jobs.
- Persistent full-history clone (not per-job shallow checkout): branch resolution
  needs history for `merge-base --is-ancestor` / `cat-file -e`; a shallow clone
  breaks the `merged`/`tip` cases. Persistent cache also avoids re-downloading a
  large repo every run.
- On-demand private fetch: point `base_repo` at the cache clone and give its
  `origin` auth via a git credential helper echoing `GH_TOKEN`, so
  `resolve_triage_branch`'s on-demand `git fetch origin <branch>` works without
  persisting a token in `.git/config`.
- Branch worktrees already work unchanged (`resolve_triage_branch` +
  `triage_worktree`); the only change is pointing `base_repo` at the cache and
  ensuring auth for the on-demand fetch. Per-issue concurrency serializes; cross
  runs stay isolated by per-uuid worktrees over a shared object DB.

Project-specific rollout (secret creation, runner `.env` edits, deleting on-disk
keys) is tracked outside this repo.

## Operations (done)

- Document outage recovery procedures in `docs/OPERATIONS.md` (replay is already
  covered by the Replay Triage / Rollback sections).
- Add local e2e coverage for triage reconcile and notify event collection.

Keep project-specific rollout notes outside this repo.
