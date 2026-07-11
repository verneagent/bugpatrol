# CI-failure feedback loop (design)

Status: design approved 2026-07-12 (cap=3, escalate→PR reviewer, re-run all
builds). Implementation pending. **Prerequisite: the fix + revise runners must
be deployed on the target repo first** (see "Prerequisite" below) — the loop
rides on `bugpatrol/fix-issue-N` PRs, which only exist once fix is live.

## Goal

When the project's own PR CI fails on a BugPatrol auto-fix PR
(`bugpatrol/fix-issue-N`), BugPatrol picks up the failure, feeds the failing
logs to the revise agent, edits code, and fast-forward pushes to the same
branch — bounded retries, stateless, cross-machine. This is the third revise
trigger alongside *review feedback* and *target-branch conflict*.

It stays decoupled from the project's CI **logic**: BugPatrol only reads the
*result surface* (a workflow's conclusion + its failed-step logs), never the
project's build definition — the same philosophy as `[fix.verify]` (exit codes)
and revise (unresolved threads).

## Trigger — `workflow_run`

```yaml
on:
  workflow_run:
    workflows: ["FiveD iOS Build","FiveD Android Build","FiveD Web Build","FiveD API Tests","FiveD E2E Tests"]
    types: [completed]
```

Job gate: `conclusion == 'failure'` && `event == 'pull_request'` &&
`startsWith(head_branch, 'bugpatrol/fix-issue-')`. Resolve the issue number from
the head branch and read `head_sha` from the event.

Notes:
- `workflow_run` only fires from the workflow file on the **default branch**.
- The `workflows:` list must be **static strings** (no expressions), so the
  deployed copy hard-codes the project's build-workflow names; the template in
  this repo ships placeholder names + a "replace me" comment.
- `check_suite` was rejected: `workflow_run` pinpoints *which* workflow failed
  and makes log retrieval a one-liner (`gh run view <id> --log-failed`).

## De-dupe on `head_sha`, not `run_id`

One revise push triggers several build workflows; if 3 fail we get 3
`workflow_run` failure events. Keying idempotency on `run_id` would burn 3 retry
attempts for one commit. Instead key on **`head_sha`**:

- Meta lives in a `BUGPATROL_CI_FIX_META` PR comment: `{attempts, last_fixed_sha}`
  (same fingerprint-comment pattern as triage/fix meta).
- On an event, read meta: `head_sha == last_fixed_sha` ⇒ this commit's CI was
  already reacted to (a fix is in flight or done) ⇒ `ci_already_handled`, skip.
- On the first reaction to a sha, gather **all** failed runs for that sha
  (`gh run list --commit <sha>` → each `gh run view <id> --log-failed`,
  truncated to the key error region) so the agent gets the full failure context
  in one turn.

The shared GitHub concurrency group `bugpatrol-fix-${repo}-${issue}`
(`cancel-in-progress: false`) serializes fix/revise/ci-fix for the same issue
across the pool; serialization + sha-keyed meta ⇒ exactly one reaction per sha.

## Bounded retries (anti-thrash)

- `[fix.gate].max_ci_fix_attempts` (default **3**, parsed via `_num`, `<=0`
  rejected).
- `attempts >= cap` ⇒ **do not edit**; escalate to a human (Lark-first
  @reviewer, then PR comment) ⇒ `ci_fix_escalated`. The reviewer is the PR's
  assignee (the CODEOWNERS owner set at fix time).
- Otherwise: revise worktree off `origin/<branch>` → `_run_fix_agent` with the
  CI logs as feedback → **apply the normal diff-size / protected-path gate** (a
  CI fix is an in-scope edit, unlike the conflict-merge path which skips it) →
  run `[fix.verify]` → commit `[ci-fix]` → fast-forward push → increment
  `attempts`, write `last_fixed_sha` → notify → `ci_fixed`.
- The standing PR labels re-run all builds on the new commit; still failing ⇒
  attempt N+1 ⇒ at the cap it escalates. Bounded loop.

Re-run scope: **all** builds re-run on each fix push (the standing-label
behavior of the label-adder); we do not try to re-run only the failed one.

## Statuses

`no_ci_failure` / `ci_already_handled` / `ci_fixed` / `ci_fix_escalated` /
`verify_failed` / `blocked`.

## Changes (bugpatrol repo)

1. `config.py` — `[fix.gate].max_ci_fix_attempts` (default 3, `_num`).
2. `github.py` / `clients.py` — `list_failed_runs_for_sha`,
   `get_run_failed_logs`, read/write `BUGPATROL_CI_FIX_META`.
3. `fix_result.py` — render CI-failure instructions for the agent, escalation
   PR comment + Lark message, `notify_ci_fix` / `notify_ci_escalation`
   (Lark-first, marker-last).
4. `fix_runner.py` — `run_ci_fix` / `execute_ci_fix`, reusing
   `fix_revise_worktree` + `_run_fix_agent`.
5. `__main__.py` — `run-ci-fix --issue N --head-sha SHA --repo-path
   --output-dir [--execute]` (separate from revise: different input + de-dupe).
6. `examples/github-actions/bugpatrol-ci-fix.yml` — template
   (`on: workflow_run [completed]`, placeholder workflow names, shares the fix
   concurrency group).
7. `docs/FIX-RUNNER.md` — cross-link this loop + new statuses.
8. Tests — config (cap default/reject), fix_runner (sha de-dupe, cap→escalate,
   success→ci_fixed), fix_result (renderers + Lark-first ordering), github (log
   fetch / meta parse).

## Prerequisite — fix + revise must be live on the target repo

As of 2026-07-12 the fix runner and revise are **not deployed on fived** (only
triage / reconcile / close-audit / notify-fix are; `notify-fix` is a
notification-only `workflow_dispatch`, not the fix runner). Until fix opens
`bugpatrol/fix-issue-N` PRs on fived, both `bugpatrol-pr-ci.yml` (already
deployed) and this loop are inert there. Path A (do this first): deploy
`bugpatrol-fix.yml` + `bugpatrol-fix-revise.yml` to fived, add `[fix.verify]` /
`[fix.gate]` to `projects/fived.toml`, and validate fix→pr-ci end-to-end on a
real issue before wiring the CI-fix loop on top.
