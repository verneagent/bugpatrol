# BugPatrol Next Plan

This plan covers the remaining product work after the current intake, PRD
context, triage dry-run, and sandbox live apply slices.

## Current Baseline

- `main` is green on CI at commit `8cd5e56`.
- Unit tests and local e2e tests pass.
- Sandbox live triage apply has been verified against
  `TheCloverLab/bugpatrol-todo-sandbox`.
- FiveD triage dry-run has been verified against a real issue using local
  `openspec` PRD sources.
- FiveD real apply has not been run because the current applier always appends
  a triage comment and is not yet idempotent.

## Principles

- Intake records reporter input only.
- Triage is a separate workflow.
- Fix tracking is a separate workflow from triage.
- GitHub issues, fields, comments, PRs, and commits are the durable workflow
  state.
- Lark is a notification and reporter interaction surface.
- Hidden metadata is used only for idempotency, backlinks, and external message
  references.
- Every meaningful workflow needs unit tests, local e2e tests, and opt-in live
  sandbox e2e tests.

## Phase 1: Needs Info And Follow-Up Loop

Goal: support the common case where triage cannot finish without asking the
reporter for more information.

### Product Behavior

1. Triage result may return `triage_status = "Needs info"` and non-empty
   `follow_up_questions`.
2. The applier writes GitHub fields and a GitHub triage comment.
3. The applier sends the follow-up questions back to the original Lark topic
   when Lark metadata is available.
4. New reporter messages in that same Lark topic are appended to the existing
   GitHub issue as follow-up comments.
5. A later command or automation can re-run triage using the expanded issue
   context.

### Implementation Tasks

- Add a Lark follow-up sender to triage apply.
- Read original Lark metadata from intake issue body or comments.
- Render a project-language follow-up message for `Needs info`.
- Store Lark follow-up message IDs in hidden metadata.
- Add a rerun entry point for issues that move from `Needs info` back to
  `Running`.

### Tests

- Unit: parse and validate `Needs info` triage results.
- Unit: render follow-up Lark copy.
- Unit: skip Lark send when no Lark metadata exists.
- Local e2e: triage returns `Needs info`, fake Lark receives questions, later
  fake Lark message updates the same GitHub issue.
- Live sandbox e2e: send follow-up questions to the sandbox Lark group, append
  a reporter follow-up, and verify the same sandbox issue is updated.

## Phase 2: Idempotent Triage Apply

Goal: make repeated triage apply safe on real issues.

### Product Behavior

1. Reapplying the same triage result does not create duplicate comments.
2. Reapplying may update fields to the latest validated values.
3. Existing BugPatrol triage comments are found through metadata, not fuzzy text.
4. Lark follow-up messages are not resent when already sent.

### Metadata

Use a hidden GitHub comment block:

```md
<!-- BUGPATROL_TRIAGE_META
{
  "version": 1,
  "issue": 123,
  "result_fingerprint": "...",
  "triage_comment_id": "...",
  "lark_follow_up": {
    "status": "sent",
    "message_id": "om_xxx"
  }
}
BUGPATROL_TRIAGE_META -->
```

### Implementation Tasks

- Add triage result fingerprinting.
- Add metadata reader/writer for triage comments.
- If the same fingerprint was already applied, skip comment and Lark sends.
- If a previous BugPatrol triage comment exists, update it or append a new
  revision according to a deterministic policy.
- Make `apply-triage-result` print a summary of writes and skips.

### Tests

- Unit: metadata parse/render.
- Unit: same fingerprint skips duplicate comment.
- Local e2e: apply twice and assert one triage comment.
- Live sandbox e2e: apply twice and assert no duplicate GitHub comment or Lark
  message.

## Phase 3: Sandbox Complex Scenarios

Goal: prove the product loop before touching FiveD production writes.

### Required Scenarios

- Intake create issue from Lark.
- Same topic follow-up appends GitHub comment.
- Triage `Done` writes fields/comment/assignee.
- Triage `Needs info` asks Lark follow-up questions.
- Reporter follow-up updates the same GitHub issue.
- Re-run triage after follow-up.
- Repeated apply does not duplicate comments or Lark notifications.
- Failed triage writes `Triage status = Failed` with an actionable error.

### Tests

- Keep fake/local e2e fast and deterministic.
- Keep sandbox live e2e opt-in via environment variables.
- Every live e2e must create disposable sandbox issues and close them in
  cleanup.
- Live Lark tests must use only the sample group
  `oc_d371f022f168b567a141ced142691894`.

## Phase 4: Lark WebSocket Watcher

Goal: replace or complement polling with lower-latency event ingestion.

### Product Behavior

- Receive Lark message events over WebSocket.
- Ignore bot messages and BugPatrol backlink messages.
- Deduplicate by message ID.
- Normalize events into `IntakeRecord`.
- Reuse the existing intake workflow.

### Implementation Tasks

- Add WebSocket event receiver.
- Add reconnect and heartbeat handling.
- Add durable cursor or processed-message ledger.
- Add `watch-lark-events` CLI.
- Keep polling watcher available as a fallback.

### Tests

- Unit: event normalization.
- Unit: bot/backlink filtering.
- Local e2e: fake event stream into intake workflow.
- Live sandbox e2e: optional smoke with the sandbox Lark group.

## Phase 5: Attachment And Resource Normalization

Goal: preserve evidence from Lark reports.

### Product Behavior

- Download supported Lark images, videos, and files.
- Store resources in a durable configured location.
- Link resources from GitHub issue body or comments.
- Set `Evidence` field based on actual normalized resources.

### Implementation Tasks

- Add Lark resource download client methods.
- Add storage abstraction for local/sandbox and future production storage.
- Extend `IntakeRecord` with normalized attachments.
- Render attachment sections in issue body and follow-up comments.

### Tests

- Unit: attachment type detection.
- Unit: evidence field derivation.
- Local e2e: text plus image fixture.
- Live sandbox e2e: at least one real image attachment.

## Phase 6: Fix Tracking And Lark Notifications

Goal: notify the original Lark topic when a fix PR, merge, linked commit, or
closed issue indicates progress, without duplicate notifications.

### Product Boundary

Fix tracking is not triage. It only reports repair progress for an already
created GitHub issue.

### Trigger Sources

- `pull_request.opened`
- `pull_request.ready_for_review`
- `pull_request.closed` with `merged = true`
- `issues.closed`
- Optional scheduled reconcile for missed events
- Optional commit scan for messages that reference or close issues

### Association Rules

Find related issues through:

- PR body closing keywords such as `fixes #123`.
- PR body references such as `refs #123`.
- Commit messages with closing or reference keywords.
- GitHub timeline events.
- Existing issue comments that link a PR.

Prefer explicit GitHub relationships over text guesses.

### Notification Events

- `pr_opened`: a fix PR was opened or marked ready for review.
- `pr_merged`: a fix PR was merged.
- `commit_linked`: a relevant commit was linked without a PR.
- `issue_fixed`: the issue was closed as completed or otherwise marked fixed.

### Idempotency Keys

Use stable keys:

- `pr_opened:<repo>#<pr_number>`
- `pr_merged:<repo>#<pr_number>`
- `commit:<repo>@<sha>`
- `issue_fixed:<repo>#<issue_number>`

### Metadata

Use a hidden GitHub comment block:

```md
<!-- BUGPATROL_FIX_META
{
  "version": 1,
  "issue": 123,
  "notified": {
    "pr_opened": ["TheCloverLab/fived#456"],
    "pr_merged": ["TheCloverLab/fived#456"],
    "commit_linked": ["TheCloverLab/fived@abc1234"],
    "issue_fixed": true
  },
  "lark": {
    "chat_id": "oc_xxx",
    "root_id": "om_xxx",
    "last_message_ids": {
      "pr_opened:TheCloverLab/fived#456": "om_xxx"
    }
  }
}
BUGPATROL_FIX_META -->
```

### Send Order

1. Read intake Lark metadata from the issue.
2. Read existing `BUGPATROL_FIX_META`.
3. Discover related PRs, commits, and issue state.
4. Compute pending notification events.
5. In dry-run mode, print the pending events.
6. In write mode, send each Lark message.
7. Record sent notification keys and Lark message IDs in metadata.
8. Re-run should produce no pending events for already sent keys.

### Failure Handling

- If Lark send fails, do not record the notification as sent.
- If metadata write fails after Lark send, retry metadata write.
- If retry still fails, the next run may duplicate; surface this as a failed
  state and require manual reconcile.
- Add a future reconcile command that can mark a known Lark message ID as sent.

### CLI

Add:

```bash
python3 -m bugpatrol notify-fix projects/todo-sandbox.toml \
  --issue 123 \
  --dry-run

python3 -m bugpatrol notify-fix projects/todo-sandbox.toml \
  --issue 123 \
  --write
```

Optional event-specific mode:

```bash
python3 -m bugpatrol notify-fix projects/todo-sandbox.toml \
  --issue 123 \
  --event pr_merged \
  --pr 456 \
  --write
```

### Tests

- Unit: PR and commit issue association parsing.
- Unit: fix metadata parse/render.
- Unit: duplicate notification keys are skipped.
- Local e2e: fake issue plus fake PR opened sends one Lark message.
- Local e2e: second run sends zero Lark messages.
- Local e2e: merged PR sends a separate merge notification once.
- Live sandbox e2e: create disposable issue and PR, run notify twice, verify one
  Lark notification, then close cleanup resources.

## Phase 7: GitHub Actions And Runner Automation

Goal: wire the product loops into repeatable automation.

### Workflows

- Intake watcher deployment is separate from GitHub Actions.
- Triage can run from GitHub Actions on trusted self-hosted runners.
- Fix notifications can run from GitHub Actions on PR and issue events.

### Runner Requirements

- Correct GitHub token permissions for issue fields, comments, PR reads, and
  issue updates.
- Lark app secret available only to trusted jobs.
- Codex or Claude login configured for triage jobs.
- Repo checkout path and PRD cache path configured explicitly.

### Tests

- Sandbox GitHub Actions dry-run.
- Sandbox GitHub Actions write mode after dry-run passes.
- Clear logs that distinguish skipped, written, and failed operations.

## Phase 8: FiveD Production Readiness

Goal: safely enable FiveD production workflows after sandbox proof.

### Checklist

- Triage apply is idempotent.
- Fix notifications are idempotent.
- Needs-info Lark follow-up has passed sandbox live e2e.
- GitHub Issue Fields write permissions are confirmed.
- Lark bot can send to the real FiveD bug group.
- CODEOWNERS maps likely paths to assignable GitHub users.
- Runner has a deterministic FiveD checkout and `openspec` cache path.
- No workflow depends on a developer-local path such as `~/clover/fived`.
- Failure paths write visible status rather than silently dropping work.

### First Production Smoke

1. Pick one real FiveD issue with explicit approval.
2. Run triage dry-run and inspect context.
3. Run apply in write mode.
4. Read back GitHub Issue Fields and comments.
5. Confirm no duplicate apply on second run.
6. If Lark metadata exists, send exactly one sandbox-approved style follow-up or
   fix notification.

## Open Decisions

- Whether triage comments should be updated in place or appended as revisions.
- Where to store downloaded Lark resources for production.
- Whether fix notifications should fire on PR opened, ready for review, merged,
  issue closed, or a smaller subset.
- How to reconcile a Lark notification that was sent successfully but failed to
  write metadata.
- Whether FiveD wants automatic re-triage after reporter follow-up or manual
  trigger only.
