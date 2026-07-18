# PR CI feedback loop (design)

Status: design approved 2026-07-12 (cap=3, escalate→PR reviewer, re-run all
builds); build-ready notification added 2026-07-13. **Implemented 2026-07-13**
(`run-ci-feedback`, `examples/github-actions/bugpatrol-ci-fix.yml`,
`[fix.gate].max_ci_fix_attempts`; see [FIX-RUNNER.md](./FIX-RUNNER.md) → "PR CI
feedback"). Not yet deployed on fived (needs the placeholder build-workflow names
filled in). **Prerequisite (fix + revise deployed on the target repo) is DONE on
fived as of 2026-07-13** (see "Prerequisite" below). **Generalised 2026-07-17**:
the loop now reacts to **any** PR that closes a managed issue (association via
`closingIssuesReferences`), not just `bugpatrol/fix-issue-N` PRs — a human's
manual-fix PR gets its CI results surfaced too.

One `workflow_run [completed]` listener, resolving the managed issue from the
PR's closing-issue references, then **three branches** off the run's conclusion:

- **`success`** ⇒ *build-ready notification*: the build is clean and testable, so
  surface it to the issue + the reporter's Lark topic, @ the assignee. (Applies
  to any managed-issue PR — the reason a human ever gets a testable build.)
- **`failure` on a bugpatrol fix branch** ⇒ *CI-fix loop*: feed the failing logs
  to revise, edit, push, bounded retries. (The original design below.)
- **`failure` on a human branch** ⇒ *notify-only*: BugPatrol does not revise a
  branch it does not own; it just surfaces the failing build to the topic.

## Goal (failure branch — CI-fix)

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

Job gate: `event == 'pull_request'` &&
`startsWith(head_branch, 'bugpatrol/fix-issue-')`, then branch on
`conclusion`: `failure` → CI-fix, `success` → build-ready notification (a
neutral/cancelled/skipped conclusion is a no-op). Resolve the issue number from
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

## Build-ready notification (success branch)

When a build workflow succeeds on a `bugpatrol/fix-issue-N` head, the fix is
testable. Surface it to the two places a human watches — the **issue** and the
**reporter's Lark topic** — @ the assignee (the PR reviewer). The PR itself
already carries the links (see below); the point is to pull them to where the
tester lives, not to make them hunt the PR.

**No build-registry dependency.** fived's build workflows already `gh pr comment`
the install links onto the fix PR (iOS OTA install page, Android APK, Web PR
preview). That PR — which BugPatrol already owns and talks to over `gh` — is the
result surface. We never call `build.spongekit.com` / `BUILD_REGISTRY_URL`; the
registry (keyed by pr/sha) stays an internal fived detail.

Two tiers, ship **tier 1 first**:

1. **Link to the PR (default).** Post once per sha: "✅ 修复构建通过，可测试 →
   PR #N（安装链接见 PR 评论）@assignee". Needs only *build passed + PR number* —
   zero parsing, zero external calls, zero format coupling. Costs the tester one
   click into the PR.
2. **Inline the links (upgrade).** Read the fix PR's comments, filter by the CI
   bot author + a stable marker, extract the iOS/Android/Web URLs, and inline
   them into the issue comment + Lark card. Better UX; mild coupling to the
   comment's markdown shape — if it drifts, fall back to tier 1.

De-dupe like the failure branch: key on `head_sha` in `BUGPATROL_CI_FIX_META`
(reuse `last_notified_sha`) so N green build workflows for one commit notify
**once**. A later failure→ci-fix push is a new sha ⇒ a new build-ready ping when
it goes green. Notify is **Lark-first, marker-last** (at-least-once, same as the
rest of fix/revise) ⇒ `build_notified`. It edits nothing, so it skips the diff /
verify gates entirely — it is pure notification.

## Statuses

Failure branch: `no_ci_failure` / `ci_already_handled` / `ci_fixed` /
`ci_fix_escalated` / `verify_failed` / `blocked`.
Success branch: `build_notified` / `build_already_notified` / `no_pr`.

## Changes (bugpatrol repo)

1. `config.py` — `[fix.gate].max_ci_fix_attempts` (default 3, `_num`).
2. `github.py` / `clients.py` — `list_failed_runs_for_sha`,
   `get_run_failed_logs`, read/write `BUGPATROL_CI_FIX_META`.
3. `fix_result.py` — render CI-failure instructions for the agent, escalation
   PR comment + Lark message, `notify_ci_fix` / `notify_ci_escalation`
   (Lark-first, marker-last).
4. `fix_runner.py` — `run_ci_feedback` orchestrator (resolve PR by head branch →
   resolve managed closing issue → branch on conclusion), `run_ci_fix` /
   `execute_ci_fix` reusing `fix_revise_worktree` + `_run_fix_agent`,
   `run_build_ready` (success branch, no worktree/agent — read PR + notify only),
   `_notify_human_pr_ci_failure` (human-branch failure, notify only). `run_ci_fix`
   / `run_build_ready` take resolved `(issue, pr)` from the orchestrator.
5. `__main__.py` — `run-ci-feedback --head-branch BRANCH --head-sha SHA
   --conclusion success|failure --repo-path --output-dir [--execute]` (one
   entrypoint; resolves the managed issue from the PR association).
6. `examples/github-actions/bugpatrol-ci-fix.yml` — template
   (`on: workflow_run [completed]`, placeholder workflow names, shares the fix
   concurrency group); one job that runs `run-ci-feedback` (which itself branches
   on `conclusion` + branch ownership).
7. `fix_result.py` — build-ready renderers (issue comment + Lark card, tier 1
   link-to-PR; tier 2 inline links from the PR comments) + `notify_build_ready`.
8. `docs/FIX-RUNNER.md` — cross-link this loop + new statuses.
9. Tests — config (cap default/reject), fix_runner (sha de-dupe, cap→escalate,
   success→ci_fixed, build-ready once-per-sha), fix_result (renderers +
   Lark-first ordering), github (log fetch / meta parse / PR-comment link
   extraction for tier 2).

## Prerequisite — fix + revise must be live on the target repo (DONE)

Path A is **done on fived (2026-07-13)**: `bugpatrol-fix.yml` +
`bugpatrol-fix-revise.yml` deployed (Sobit-token / cache / nvm-node
provisioning), `[fix.verify]` (`npm ci` + `scripts/preflight.sh lint`) +
`[fix.gate]` added to `projects/fived.toml`. The Sobit app already carries
contents:write + pull-requests:write on fived; node 24.14.1 is installed on all
three runners. Remaining before wiring this loop: a real fix→pr-ci end-to-end
run on a chosen issue (validates `npm ci`/preflight on the runners and the
pr-ci label cascade actually firing the builds).
