# FiveD Bug Pipeline Migration Plan

Goal: migrate from the existing `~/clover/fived` bug pipeline to `bugpatrol`
without interrupting the current FiveD Lark group or changing the current
production behavior until the new path has proven parity.

## Current Production Surface

- Lark group: existing FiveD bug group with historical topics.
- Current handler: scripts under `~/clover/fived`.
- Durable issue surface: GitHub issues in `TheCloverLab/fived`.
- Durable attachment surface: `TheCloverLab/fived-assets`.
- New bugpatrol config: `projects/fived.toml`.

## Migration Principle

Do not run two writers against the same Lark topic at the same time.

`bugpatrol` can read the same history in dry-run/shadow mode, but it should not
create/update FiveD issues or reply to Lark until a topic range is explicitly
assigned to it.

## Phase 0: Freeze Behavior and Back Up State

1. Record the current old pipeline entrypoints:
   - watcher process command
   - local state/cache files
   - GitHub labels/issue metadata it writes
   - Lark reply format
2. Export a sample of recent handled topics:
   - text-only topic
   - image first message
   - image follow-up reply
   - video follow-up reply
   - follow-up question / needs-info case
   - fixed-by-PR case
3. Keep the old pipeline running as the only writer.

Done criteria:

- We can name the old watcher process and restart/stop it safely.
- We have 10-20 representative historical topic IDs for comparison.

## Phase 1: Shadow Read Only

Run `bugpatrol` against the real FiveD group without writes:

```bash
FIVED_LARK_BUG_APP_SECRET=... \
python -m bugpatrol backfill-lark projects/fived.toml --limit 50
```

Expected behavior:

- No GitHub writes.
- No Lark replies.
- No asset uploads.
- Output only scanned/processed/skipped counters.

Then build triage context for existing issues manually:

```bash
python -m bugpatrol issue-context projects/fived.toml \
  --issue <existing-issue> \
  --repo-path ~/clover/fived \
  --output .bugpatrol/shadow/context-<issue>.md
```

Done criteria:

- `bugpatrol` can read the group history.
- Existing issues with comments produce context containing issue body, comments,
  PRD hits, and `Media Evidence`.
- No production state changes.

## Phase 2: Shadow With Local Artifacts

Use local resource materialization, not asset repo writes:

```bash
FIVED_LARK_BUG_APP_SECRET=... \
python -m bugpatrol backfill-lark projects/fived.toml \
  --limit 20 \
  --write \
  --resource-dir .bugpatrol/shadow/resources
```

Only run this against a deliberately created test topic in the FiveD group, or a
temporary private test group that uses the FiveD app credentials.

Done criteria:

- Text, image, and video messages are normalized correctly.
- Media description command works for real Lark resources.
- The generated issue/comment body is acceptable.

If this must not create a real FiveD issue, keep this phase in the sandbox repo
but use the same `TheCloverLab/fived-assets` and media command.

## Phase 3: Sandbox Writes With FiveD-Like Inputs

Continue using the sandbox GitHub repo but real assets repo:

```bash
BUGPATROL_LIVE_E2E=1 \
BUGPATROL_LIVE_ASSET_E2E=1 \
BUGPATROL_LIVE_VIDEO_E2E=1 \
BUGPATROL_TODO_LARK_APP_SECRET=... \
python -m unittest tests.e2e.test_live_asset_resource_loop
```

This verifies:

- Lark image reply -> `fived-assets` -> issue comment.
- Lark video reply -> `fived-assets` -> issue comment.
- Vision description appears in the comment.
- Test assets are removed after verification.

Done criteria:

- The live e2e passes repeatedly.
- `fived-assets` has no leftover test files.
- `~/.lark-cli/config.json` is restored after video tests.

## Phase 4: Controlled FiveD Write Pilot

Pick one new low-risk FiveD bug topic and route only that topic through
`bugpatrol`.

Recommended pilot command:

```bash
FIVED_LARK_BUG_APP_SECRET=... \
python -m bugpatrol backfill-lark projects/fived.toml \
  --limit 5 \
  --write \
  --asset-repo
```

Before running:

- Pause the old pipeline for that one topic if it has topic-level routing.
- If the old pipeline only supports global on/off, use a freshly created pilot
  topic and stop the old watcher briefly during the pilot.

After running:

- Confirm exactly one GitHub issue was created/updated.
- Confirm exactly one Lark backlink was sent.
- Confirm images/videos point to `TheCloverLab/fived-assets`.
- Confirm generated descriptions appear.
- Confirm triage context includes comments and media evidence.

Rollback:

- Stop bugpatrol.
- Restart old pipeline.
- Close/delete only the pilot issue if needed.
- Remove only pilot test assets if needed.

## Phase 5: Shadow Triage, No Apply

For a small set of real FiveD issues, generate context and run triage without
writing results:

```bash
python -m bugpatrol run-triage projects/fived.toml \
  --issue <issue> \
  --repo-path ~/clover/fived \
  --output-dir .bugpatrol/triage-<issue>
```

Done criteria:

- Context includes PRD hits from `openspec`.
- Context includes issue comments.
- Context includes `Media Evidence`.
- The agent command is correct for the configured runner.

## Phase 6: Apply Triage to One Pilot Issue

Only after Phase 5 looks good:

```bash
python -m bugpatrol run-triage projects/fived.toml \
  --issue <pilot-issue> \
  --repo-path ~/clover/fived \
  --output-dir .bugpatrol/triage-<pilot-issue> \
  --execute
```

Done criteria:

- Triage fields are written once.
- Triage comment has `BUGPATROL_TRIAGE_META`.
- Re-running does not duplicate the comment.
- Needs-info triage replies to Lark once.

## Phase 7: Replace the Watcher

Only replace the old watcher after intake and triage have passed the pilot.

Preferred command:

```bash
FIVED_LARK_BUG_APP_SECRET=... \
python -m bugpatrol watch-lark projects/fived.toml \
  --interval 30 \
  --limit 20 \
  --asset-repo
```

Run it under the chosen process manager, for example systemd, launchd, tmux, or
the existing FiveD runner mechanism.

Done criteria:

- One active writer only.
- Restart procedure documented.
- Logs are captured.
- Secret injection is documented.
- Backfill/replay procedure is documented.

## The Four Automation Questions

These were shorthand for production wiring decisions:

1. Runner environment
   - Which machine/user runs `watch-lark`, `run-triage`, and `notify-fix`?
   - Does that user have `codex login`, `gh auth`, `lark-cli`, `ffmpeg`, and
     the vision API key?

2. GitHub and Lark credentials
   - Which env vars are provided?
   - Which GitHub account writes issues and pushes to `fived-assets`?
   - Which Lark app sends replies and downloads resources?

3. Owner mapping
   - Which CODEOWNERS file or mapping decides assignee?
   - What happens when no owner matches?

4. Write permissions
   - Can the runner create/update issues, Issue Fields, assignees, comments,
     and push to `TheCloverLab/fived-assets`?
   - Can it send Lark replies and download message resources?

## What Must Stay Off Until Cutover

- A second always-on Lark watcher in the real FiveD group.
- Automatic triage apply on every FiveD issue.
- Automatic fix notifications on the real FiveD repo.

These can be tested in sandbox or one pilot issue first.
