# bugpatrol TODO

Project-neutral implementation items:

## Reconcile

- Add `reconcile-triage` command.
  - Scan only issues with `BUGPATROL_INTAKE_META`.
  - Dispatch triage for `Pending`, `Needs review`, and stale `Running` issues.
  - Respect the triage coalescing quiet window before dispatching.
  - Use active-run metadata or an equivalent fingerprint to avoid duplicate
    dispatches.
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

## Branch attribution

- Parse deterministic build metadata (e.g. a `branch: xxx` line) from Lark app
  reporter messages at intake, once the app-side report format is settled.
  Stamp it into `BUGPATROL_INTAKE_META` so triage gets ground truth instead of
  relying on natural-language inference.
- Consider gating `commit_linked` fix events on the branch containing the
  commit (currently only `pr_opened`/`pr_merged` are branch-gated).

## Operations

- Add stale `Running` thresholds to config with conservative defaults.
- Document replay and outage recovery procedures in `docs/OPERATIONS.md`.
- Add local e2e coverage for triage reconcile and notify event collection.

Keep project-specific rollout notes outside this repo.
