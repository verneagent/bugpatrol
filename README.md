# bugpatrol

`bugpatrol` is a project-neutral bug intake, triage, and notification toolchain.
It turns Lark bug conversations into durable GitHub issue workflow state, then
runs deterministic triage and fix-notification steps around that state.

Project-specific values live in `projects/*.toml`. Product-specific migration
plans, credentials, group IDs, and rollout notes should not live in this repo.
Use `projects/minimal.example.toml` for the smallest valid config and
`projects/full.example.toml` for optional assets, media, owners, and classifier
settings.

## Principles

- Intake records what the reporter said. It does not triage.
- Triage is a separate workflow that reads GitHub issue state and local project
  context, then writes validated fields and comments.
- Fix notification is separate from triage.
- GitHub issues, comments, native Issue Type, Issue Fields, assignees, PRs, and
  commits are the durable workflow surface.
- `BUGPATROL_INTAKE_META` is the worker ownership boundary. Triage and notify
  workers only write issues that contain it.
- Other hidden metadata is used for idempotency and backlinks.
- Lark is the reporter interaction surface, not the durable source of truth.

## Architecture

```mermaid
flowchart LR
  lark[Lark group]
  watcher[watcher daemon]
  issue[GitHub issue/comment]
  triage[triage runner]
  fields[Issue Type + Issue Fields<br/>assignee + triage comment]
  event[PR / commit / issue event]
  notify[notify runner]
  reconcile[scheduled reconcile scanner]
  lark_notice[Lark notification]

  lark --> watcher
  watcher --> issue
  watcher --> triage
  issue --> triage
  triage --> fields
  event --> notify
  fields --> notify
  notify --> lark_notice
  issue --> reconcile
  event --> reconcile
  reconcile --> triage
  reconcile --> notify
```

## Workflows

### watcher

`watcher` is a long-running daemon. It polls or receives Lark messages, normalizes
them into intake records, and creates or updates one GitHub issue per Lark topic.

Typical polling command:

```bash
python -m bugpatrol watch-lark projects/example.toml \
  --interval 30 \
  --limit 20 \
  --asset-repo \
  --event-log .bugpatrol/watch-events.jsonl \
  --lease-file .bugpatrol/watch-lark.lock \
  --processed-ledger .bugpatrol/processed-messages.json \
  --triage-queue .bugpatrol/triage-queue.json \
  --triage-dispatch-command \
    gh workflow run bugpatrol-triage.yml \
      -f issue_number={issue_number} \
      -f trigger_fingerprint={trigger_fingerprint} \
      -f reason={reason}
```

Run exactly one active watcher writer per Lark group. Multiple watcher writers
against the same group can race and create duplicate issues or comments.

When `--triage-queue` is set, watcher coalesces triage requests per issue. The
default quiet period is 60 seconds. Additional material Lark follow-ups during
that window extend the request instead of dispatching duplicate triage jobs.

Event-stream mode consumes Lark event NDJSON from stdin. Use this behind a
WebSocket event client while keeping polling as a fallback:

```bash
lark-cli event listen --format ndjson \
  | python -m bugpatrol watch-lark-events projects/example.toml \
      --asset-repo \
      --event-log .bugpatrol/watch-events.jsonl \
      --processed-ledger .bugpatrol/processed-messages.json \
      --triage-queue .bugpatrol/triage-queue.json
```

### triage

`triage` is a one-shot job. It reads an issue, comments, local PRD/docs, media
evidence, and owner data; invokes the configured agent provider; validates the
JSON result; then writes GitHub fields, assignee, and a triage comment.
It only writes issues that contain `BUGPATROL_INTAKE_META`; older project issues
without that metadata are rejected before status fields, comments, or output
files are written.

Typical command:

```bash
python -m bugpatrol run-triage projects/example.toml \
  --issue 123 \
  --repo-path /path/to/project \
  --output-dir .bugpatrol/triage-123 \
  --execute
```

This is a good fit for GitHub Actions self-hosted runners because it needs local
repo context and pre-authenticated agent tooling such as `codex login`.

See `examples/github-actions/bugpatrol-triage.yml` for a project-neutral
workflow template. Copy it into the target app repo and set
`BUGPATROL_PROJECT_CONFIG` to the local project config path on the runner.

### notify

`notify` is a one-shot job. It reports fix progress from PRs, merges, linked
commits, or issue closure back to the original Lark topic, with idempotency.
It only sends notifications for issues that contain `BUGPATROL_INTAKE_META`;
reconcile skips older project issues without that metadata.

Typical command:

```bash
python -m bugpatrol notify-fix projects/example.toml \
  --event pr_merged \
  --pr 123 \
  --write
```

This can run on GitHub-hosted or self-hosted runners, depending on where the
required GitHub and Lark credentials are available.

Scheduled reconcile jobs can replay missed events from a JSON array:

```bash
python -m bugpatrol reconcile-fix-notifications projects/example.toml \
  --input .bugpatrol/fix-events.json \
  --write
```

See `examples/github-actions/bugpatrol-notify-fix.yml` for a workflow template
covering PR opened, PR merged, issue closed, and manual notification runs.

### reconcile

`reconcile` is a scheduled scanner. It is not the primary worker for Lark
intake, triage, or notification. It periodically reads durable GitHub state and
replays work that should have happened while a runner, webhook, workflow, or
event dispatch path was down.

Recommended runtime:

```yaml
on:
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch:
```

Triage reconcile scans BugPatrol-managed issues, then dispatches
`bugpatrol-triage.yml` for issues that still need triage:

- has `BUGPATROL_INTAKE_META`
- `Triage status` is `Pending` or `Needs review`
- or `Triage status` is `Running` but the active run is stale
- was not updated inside the coalescing quiet window

Notify reconcile scans recent PRs, commits, issue timelines, and closed issues,
then dispatches `bugpatrol-notify-fix.yml` or calls `notify-fix` for fix events
that have not been announced yet:

- target issue has `BUGPATROL_INTAKE_META`
- candidate event is `pr_opened`, `pr_merged`, `commit_linked`, or `issue_fixed`
- issue does not already contain the matching `BUGPATROL_FIX_META` key

Reconcile must be idempotent. It should be safe to run every few minutes and
safe to rerun manually after an outage.

## Runner vs Daemon

| Workflow | Form | Recommended runtime | Multiplicity |
| --- | --- | --- | --- |
| watcher | daemon | systemd, launchd, tmux, or existing service host | one active writer per Lark group |
| triage | one-shot job | GitHub Actions self-hosted runner | multiple runners are OK |
| notify | one-shot job | GitHub Actions hosted or self-hosted runner | multiple runners are OK |
| reconcile | scheduled scanner | GitHub Actions scheduled workflow | one logical scanner per project is enough; reruns must be idempotent |

GitHub Actions self-hosted runners can be multiple. GitHub assigns each job to
one matching runner. The thing that must not be duplicated is the active Lark
watcher writer for the same group.

The four project entry points are:

- `watcher` daemon: Lark messages to GitHub issues/comments.
- `bugpatrol-triage.yml`: one issue to triage result.
- `bugpatrol-notify-fix.yml`: one fix event to Lark notification.
- `bugpatrol-reconcile.yml`: scheduled GitHub scan that backfills missed triage
  and notification work.

## Smoke Testing

Use local e2e tests for the product contract before calling real systems:

```bash
python -m pytest tests/e2e/test_intake_loop.py
python -m pytest tests/e2e/test_attachment_intake_loop.py
python -m pytest tests/e2e/test_fix_notify_loop.py
```

The project-neutral intake/media smoke should cover:

- a non-BugPatrol Lark app or user creates a topic
- a text follow-up in the same topic appends a GitHub issue comment
- an image or video follow-up is copied to the configured asset store
- media description is written into the issue comment
- material follow-up after `Done` or `Skipped` marks `Triage status` as
  `Needs review`
- the watcher replies back to the same Lark topic with create/update backlinks
- repeated scans skip already-processed messages through the ledger

Live smoke is opt-in. Run it only against a sandbox group/repo or a real project
that has explicitly approved the test artifacts. A minimal live run is:

1. Start one watcher daemon with `--asset-repo`, `--processed-ledger`, and
   `--triage-queue`.
2. Send a root Lark topic from a non-BugPatrol sender.
3. Send a text reply in the same topic before the quiet window expires.
4. Verify one GitHub issue is created and the reply becomes one issue comment.
5. Verify the watcher sends `已创建 GitHub issue` and `已追加到 GitHub issue`
   backlinks into the original Lark topic.
6. Wait for the coalesced triage dispatch and verify the triage workflow writes
   fields, assignee, and triage comment.
7. Send an image or video reply after triage is `Done`.
8. Verify the issue gets a new media comment, the asset URL points at the
   configured asset repo, media description is present, and `Triage status`
   becomes `Needs review`.
9. Verify the queue dispatches one follow-up triage run after the quiet window
   and repeated watcher scans do not duplicate comments or Lark replies.

## Issue State

### Native Issue Type

`bugpatrol` uses GitHub native Issue Type:

- `Bug`
- `Feature`
- `Task`

### Triage status

Current `Triage status` values:

- `Pending`: intake created or updated the issue; triage has not completed.
- `Running`: triage is executing.
- `Needs info`: triage needs more reporter information.
- `Needs review`: material new evidence arrived after triage completed, or while
  the active triage run was using an older context.
- `Done`: triage completed and wrote fields/comment/assignee.
- `Failed`: triage execution failed.
- `Skipped`: triage was intentionally skipped.

### State flow

```mermaid
stateDiagram-v2
  [*] --> Pending: Lark intake creates issue
  Pending --> Running: triage starts
  Running --> Done
  Running --> NeedsInfo: needs reporter info
  Running --> Failed
  Pending --> Skipped: skipped by policy
  Done --> NeedsReview: material follow-up
  Skipped --> NeedsReview: material follow-up
  NeedsInfo --> Pending: reporter follow-up

  state "Needs info" as NeedsInfo
  state "Needs review" as NeedsReview
```

Follow-up Lark messages always append to the existing GitHub issue when the
`chat_id + root_id` backlink matches. Not every follow-up should retrigger
triage. The planned classifier should distinguish acknowledgements, fix/status
chatter, and material new evidence.

## Fields

Canonical logical fields:

| Field | Values |
| --- | --- |
| `Priority` | `Urgent`, `High`, `Medium`, `Low` |
| `Triage status` | `Pending`, `Running`, `Needs info`, `Needs review`, `Done`, `Failed`, `Skipped` |
| `Source` | `Lark`, `GitHub`, `Manual`, `Import` |
| `Intake version` | `v2`, `manual`, `unknown` |
| `Triage verdict` | `代码 Bug`, `PRD 错误`, `PRD 缺失`, `Case 错误`, `信息不足`, `预期行为` |
| `Platform` | `iOS`, `Android`, `Web`, `Desktop`, `多平台`, `未知` |
| `Reproducibility` | `必现`, `偶发`, `仅一次`, `未知` |
| `Other platforms` | `其他平台正常`, `其他平台也异常`, `未验证`, `不适用` |
| `Capability` | `Auth`, `Quest`, `Buddy`, `Match`, `Message`, `Me`, `Contacts`, `Notifications`, `Unknown` |
| `Evidence` | `截图`, `视频`, `日志`, `文字描述`, `多种`, `无` |
| `PRD status` | `已对齐`, `PRD 错误`, `PRD 缺失`, `未校验` |
| `Triage confidence` | `高`, `中`, `低` |
| `Owner reason` | `CODEOWNERS`, `Lark @mention`, `Git history`, `Capability fallback`, `Manual` |
| `Blame` | optional text field for a best-effort person, PR, commit, or code-area regression blame suggestion |

Project configs map these logical names to the actual GitHub Issue Field names.

## Metadata

`bugpatrol` writes hidden HTML comments for worker ownership, idempotency, and
backlinks:

- `BUGPATROL_INTAKE_META`: marks an issue as BugPatrol-managed and records the
  source Lark chat/root/message and attachment URLs.
- `BUGPATROL_INTAKE_REPLY_META`: later Lark follow-up message references.
- `BUGPATROL_TRIAGE_META`: applied triage result fingerprint.
- `BUGPATROL_FIX_META`: sent fix notification keys.

These blocks are machine state. User-visible status should stay in GitHub native
fields and comments.

## Media

Lark image, video, and file resources can be materialized locally or uploaded to
a configured asset repository. Optional redaction, video frame extraction,
image resizing, byte limits, and video duration limits run before upload and
vision. When a media description command is configured, resources are described
before issue/comment rendering so triage can read the generated text.

Default media command:

```bash
python -m bugpatrol.media_vision <image-or-video-path> [question]
```

It uses an OpenAI-compatible multimodal API configured by:

- `~/.bugpatrol/vision.json`
- `BUGPATROL_VISION_*` environment variables
- `~/.lark-bug-watcher/vision.json` as a compatibility fallback

Asset cleanup is dry-run by default:

```bash
python -m bugpatrol cleanup-assets projects/example.toml --message-id-prefix om_live_test
python -m bugpatrol cleanup-assets projects/example.toml --message-id-prefix om_live_test --delete --push
```

## Development

Operational guides:

- `docs/RUNNER.md`
- `docs/DAEMON.md`
- `docs/OPERATIONS.md`

```bash
python -m unittest discover -s tests
python -m unittest discover -s tests/e2e
python -m compileall bugpatrol tests
python -m bugpatrol schema
python -m bugpatrol validate-config projects/example.toml
python -m bugpatrol validate-config projects/full.example.toml
```

Useful commands:

```bash
python -m bugpatrol doctor projects/example.toml
python -m bugpatrol backfill-lark projects/example.toml --limit 20
python -m bugpatrol backfill-lark projects/example.toml --limit 20 --write --asset-repo
python -m bugpatrol issue-context projects/example.toml --issue 1 --repo-path /path/to/project
python -m bugpatrol run-triage projects/example.toml --issue 1 --repo-path /path/to/project
python -m bugpatrol apply-triage-result projects/example.toml --issue 1 --input triage-output.json --dry-run
python -m bugpatrol notify-fix projects/example.toml --event pr_opened --pr 12
```

## Live Tests

Live tests are opt-in and must target disposable sandbox resources.

```bash
BUGPATROL_LIVE_E2E=1 \
BUGPATROL_TODO_LARK_APP_SECRET=... \
python -m unittest tests.e2e.test_live_intake_loop

BUGPATROL_LIVE_E2E=1 \
BUGPATROL_LIVE_ASSET_E2E=1 \
BUGPATROL_LIVE_VIDEO_E2E=1 \
BUGPATROL_LIVE_ASSET_REPO=example-org/example-assets \
BUGPATROL_LIVE_ASSET_CHECKOUT=~/example-assets \
BUGPATROL_TODO_LARK_APP_SECRET=... \
python -m unittest tests.e2e.test_live_asset_resource_loop
```

Live tests must clean up test issues and test assets where the platform allows.
