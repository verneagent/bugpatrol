# Fix Runner (design)

Status: design approved 2026-07-11, sandbox implementation pending.

The Fix Runner turns a triaged bug into a reviewable pull request. Triage
produces "root cause + `file:line` + owner"; the Fix Runner **consumes that
verdict**, lets an agent edit code inside a worktree of the target branch, runs
the project's own verification, and opens a PR. It **never merges** — a human
owner reviews and merges.

It reuses the Triage Runner skeleton wherever possible and only adds a new brain
(fix prompt/schema), new landing actions (open PR), and a stricter safety gate.

## Reuse from Triage Runner

Nearly unchanged:

- `worktree.py` 5-way branch resolution and the ephemeral `triage_worktree`.
- `agents.py` command assembly: `--dangerously-skip-permissions`, `--add-dir`,
  `stdin=DEVNULL`, `stream-json`, `parse_claude_token_usage`,
  `detect_sandbox_denial`.
- Intake meta, GitHub concurrency group, Lark reply notification path.
- `bugpatrol-cache-bootstrap.sh`, in-workflow token mint, Python-version probe.

New:

- `fix_runner.py` (prepare + execute), `fix_result.py` (open PR / land),
  `fix_gate.py` (safety gate), the fix prompt + fix output schema, and
  `examples/github-actions/bugpatrol-fix.yml`.

## Trigger — manual gate only (no auto-run after triage)

Editing code is a hard-to-reverse write, so fix does not chase triage
automatically. Three human-gated entry points:

1. Add label `bugpatrol-fix` to an issue → `workflow_dispatch`.
2. Reply "修一下" / "/fix" in the Lark topic → watcher dispatches a fix job.
3. Triage may flag `auto_fixable=true`, but that only sends a Lark "可自动修，
   回复确认" prompt — it does not act on its own.

A fully-automatic allow-listed lane is intentionally out of scope until the
manual lane is proven.

## Inputs — reuse the triage verdict

Triage already wrote root cause, reproduction, `file:line`, and owner into the
issue-comment `BUGPATROL_TRIAGE_META`. Fix reads it back instead of re-analyzing.
`fix-context.md` contains:

- Triage root cause + reproduction + the cited `file:line` anchors.
- Target-branch source (agent `cwd` = the worktree, edited in place).
- The module's CODEOWNERS owner (used as PR reviewer).
- Relevant test paths.
- Constraints: only touch the cited module, always add/adjust tests, never touch
  CI, secrets, or lockfiles.

## Execution + verification gate — driven by project config, not BugPatrol

BugPatrol must not hard-code any build/test/lint command. Each project declares
its own in its TOML:

```toml
[fix.verify]
build = "pnpm build"          # optional
typecheck = "pnpm typecheck"  # optional
test = "pnpm test -- <scope>" # optional
lint = "pnpm lint"            # optional
```

`execute_fix_run`:

1. `subprocess` `claude -p` with `cwd = worktree`; the agent edits code and
   adds/adjusts tests.
2. Run each configured verify command **in the worktree**, exit code is the
   verdict. Any non-empty command that fails ⇒ do **not** open a PR; report a
   failure summary to Lark and fail loud.
3. `detect_sandbox_denial` hit ⇒ `mark_failed` immediately (denial is a config
   error, retry is useless).

BugPatrol only runs whatever is configured and reads exit codes — zero coupling
to any project's toolchain. If nothing is configured it skips the gate, but at
least one verify command must be non-empty or the run is rejected (an unverified
auto-fix is not shippable).

## Landing — open a PR, never merge

1. `git checkout -b bugpatrol/fix-issue-N` off the triage target branch.
2. Commit (signed as `sobit-bot`, message references the issue + root cause).
3. `git push` + `gh pr create --base <target-branch>`.
4. PR body: root cause + change summary + verification results (all green) +
   "AI-generated, requires human review".
5. `gh pr edit --add-reviewer <CODEOWNERS owner>`.
6. Issue comment + Lark notification (@owner to review) with the PR link.
7. Write `BUGPATROL_FIX_META` (fingerprint) for idempotency.

## Safety gate — stricter than triage

Any one of these blocks the run (no edit / no PR):

- Triage verdict ∈ {needs-info, duplicate, not-a-bug}.
- Triage gave no concrete `file:line` anchor.
- Diff touches `.github/`, secrets, lockfiles, or migration scripts.
- Diff exceeds `[fix.gate].max_diff_lines` (default **800**).
- Changed files fall outside the triage-cited module.
- Verification gate not all-green.
- An open `bugpatrol/fix-issue-N` PR already exists (idempotent; report the link).

## Concurrency & multi-runner isolation

The Fix Runner is designed for a **pool of runners**, including **several runners
on one physical device**, without stepping on each other. Two layers:

### Layer 1 — inherited from the Triage model (git/checkout isolation)

- **Distinct `BUGPATROL_RUNNER_NAME` per runner instance.** The cache clone is
  namespaced `$HOME/.bugpatrol-cache/$RUNNER_NAME/<owner>/<repo>`, so two runners
  on one box never share a clone. Startup fails loud if two runners resolve to
  the same name (colliding cache path).
- **Single-concurrency per runner.** A self-hosted runner runs one job at a time,
  so every git / worktree op is serial and race-free *within* a runner.
- **GitHub concurrency group per issue** (`bugpatrol-fix-${repo}-${issue}`,
  `cancel-in-progress: false`): the same issue serializes; different issues run
  in parallel across the pool.
- **Branch names keyed by issue** (`bugpatrol/fix-issue-N`): distinct issues =
  distinct branches, so cross-runner pushes never collide.

### Layer 2 — new, fix-specific (OS-global resource isolation)

Triage never built anything; fix runs the project's build/test/lint, which touch
resources shared across runners on the same device. Additional isolation:

- **Ephemeral per-run worktree** under the runner-owned scratch, named with a
  UUID and removed in `finally`. Working dirs (and their build artifacts) are
  isolated per run and, since each runner has its own cache clone, per runner.
- **Per-run `TMPDIR`** so temp files from concurrent fix builds never collide.
- **Per-runner package-manager caches** via env keyed by `$RUNNER_NAME`
  (e.g. `npm_config_cache`, `PNPM_HOME`/store dir, `pip` cache), avoiding
  concurrent-write races in a shared global store.
- **Device-level fix semaphore** — optional `[fix.runner].max_concurrent_per_device`
  (lockfile counting semaphore). Two+ runners on one box will not all run heavy
  builds at once; excess fix jobs wait rather than thrash CPU/RAM.
- **Scarce OS resources in verify commands** (fixed ports, shared test DBs) are
  the project's responsibility: verify commands should bind ephemeral ports and
  use per-run temp state. BugPatrol documents this contract; it cannot enforce a
  project's toolchain choices.

### Recommended topology

- A dedicated label `bugpatrol-fix` (separate pool from `bugpatrol-fived-triage`)
  so heavy fix builds don't starve light triage jobs.
- One device may host a triage runner and one-or-more fix runners, each with a
  unique `BUGPATROL_RUNNER_NAME`.

Recommended workflow template:

```text
examples/github-actions/bugpatrol-fix.yml
```
