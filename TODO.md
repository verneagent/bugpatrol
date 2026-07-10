# bugpatrol TODO

Project-neutral implementation items:

## Reconcile

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

## Branch per topic group

Goal: some issues repro on a feature-branch build, not main. Do NOT make triage
infer the branch. Instead, one Lark topic group represents one feature branch;
the poster picks the group, so the branch is declared, not detected. Triage then
analyzes against that branch.

- Config: declare branch-scoped chats. The main `[lark].chat_id` defaults to
  `main`; add `[[lark.branch_chats]]` entries mapping `chat_id -> branch`.
- Watcher (single process): scan the main chat plus all `branch_chats` in one
  loop. Dedup already keys on `chat_id`, so groups stay isolated. Tag each new
  issue with its source group's branch.
- Intake metadata (source of truth): stamp `target_branch` into
  `BUGPATROL_INTAKE_META`. Also record a best-effort `branch_tip_sha` via
  `git ls-remote origin refs/heads/<branch>` so a later-deleted branch can still
  be classified as merged vs abandoned.
- Visible mirror: write a `text`-type org issue field "Branch" (not
  single_select — branches are dynamic) at intake for humans to see/filter. Add
  `Branch = "Branch"` to `[issue_field_names]`. Triage still reads the meta, not
  the field.
- Triage isolation: run each triage in an ephemeral detached `git worktree` off
  `origin/<branch>` at a unique path (`worktree add --detach`), then
  `worktree remove --force` in a `finally`; `worktree prune` on startup. Shares
  the object DB (cheap) and gives each run its own working tree + index, so
  concurrent runs cannot override each other regardless of runner scheduling.
  `run-triage` owns this (it learns the branch only after reading the issue).
- Triage branch resolution (handles a merged+deleted branch):
  1. Branch exists on remote -> worktree @ `origin/<branch>`.
  2. Branch gone AND `branch_tip_sha` is an ancestor of `origin/main`
     (`git merge-base --is-ancestor`) -> merged; worktree @ `origin/main`,
     annotate "feature/x merged into main".
  3. Branch gone AND SHA not in main / no SHA -> try worktree @ the recorded SHA
     (often still fetchable while the PR exists); else fall back to `origin/main`
     with a strong caveat (branch deleted, not confirmed merged) and optionally
     lower confidence / mark needs-info.
  4. `target_branch` == main or absent (legacy issues) -> worktree @
     `origin/main`. Same code path, no branching in logic.
- Reporting: show the analyzed branch in the triage comment header and the Lark
  notify so nobody assumes it was main.
- Ops: retire a `[[lark.branch_chats]]` entry (archive the group) after its
  branch merges. The system tolerates lag — a late topic in a retired group
  still lands on main via resolution case 2.
- Follow-up replies inherit the root issue's branch (meta already fixed; do not
  re-decide per reply).
- Consider gating `commit_linked` fix events on the branch containing the
  commit (currently only `pr_opened`/`pr_merged` are branch-gated).

## Operations

- Add stale `Running` thresholds to config with conservative defaults.
- Document replay and outage recovery procedures in `docs/OPERATIONS.md`.
- Add local e2e coverage for triage reconcile and notify event collection.

Keep project-specific rollout notes outside this repo.
