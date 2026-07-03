# bugpatrol

Bug intake and triage orchestration for Five Degrees-style projects.

`bugpatrol` is intentionally project-neutral. It owns the platform code for:

- mirroring Lark topics into GitHub issues without doing product triage;
- storing structured facts in GitHub native issue fields and Issue Fields;
- running triage agents on trusted self-hosted runners using subscription auth
  where possible;
- writing deterministic GitHub updates and Lark follow-ups from validated JSON.

Project-specific values live in `projects/*.toml`. The first project config is
`projects/fived.toml`.

Issue intake language is project-specific:

```toml
[intake]
language = "zh-CN" # or "en-US"
```

The language controls the GitHub issue body and follow-up comment copy emitted
by the intake workflow. `projects/todo-sandbox.toml` uses Chinese.

## Architecture

The default flow is:

1. Intake watcher receives a Lark topic or backfill record.
2. The watcher creates or updates one GitHub issue per Lark root topic.
3. GitHub Actions on a self-hosted runner starts the triage runner.
4. The runner calls the configured triage provider, for example
   `codex exec --output-schema ...` using the runner user's existing Codex
   subscription login, or a Claude CLI command on a Claude-authenticated runner.
5. A deterministic field writer validates the JSON and writes GitHub native
   issue type, Issue Fields, assignee, comments, and Lark follow-up messages.

The watcher must not decide whether the report is a code bug. DeepSeek may be
used only for lossy intake helpers such as title drafts, summaries, and media
descriptions.

## Codex auth

No OpenAI API key is assumed. Run GitHub jobs on trusted self-hosted runners
where a dedicated OS user has already run:

```bash
codex login
```

Treat `~/.codex/auth.json` as a password. Do not upload it as an artifact, print
it in logs, or share it across untrusted jobs.

## Development

```bash
python -m unittest discover -s tests
python -m unittest discover -s tests/e2e
python -m bugpatrol validate-config projects/fived.toml
python -m bugpatrol validate-config projects/todo-sandbox.toml
python -m bugpatrol schema
python -m bugpatrol agent-command projects/fived.toml --issue 123
```

## Current Intake Loop

The implemented product slice is Lark intake only:

1. Receive a normalized `IntakeRecord`.
2. Find an existing GitHub issue by `chat_id + root_id`.
3. If none exists, create one issue with:
   - native issue type: `Bug`;
   - fields: `Source = Lark`, `Intake version = v2`,
     `Triage status = Pending`, and deterministic `Evidence`;
   - `BUGPATROL_INTAKE_META` backlink metadata in the issue body.
4. If the root already exists, append the new Lark message as an issue comment.
5. Reply in Lark with the GitHub issue backlink.

This layer does not triage, assign owners, compare PRD, or decide whether the
report is truly a code bug.

Reusable tests:

- Unit contract: `tests/test_intake_workflow.py`
- Local e2e loop: `tests/e2e/test_intake_loop.py`
- E2E fixture payload: `tests/fixtures/intake_topic_loop.json`

Opt-in live sandbox e2e:

```bash
BUGPATROL_LIVE_E2E=1 \
BUGPATROL_TODO_LARK_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_aac97d050d385ee9.secret)" \
python -m unittest tests.e2e.test_live_intake_loop
```

The live e2e uses only `TheCloverLab/bugpatrol-todo-sandbox` and
`Bugpatrol Todo Sandbox`. It creates a test issue, posts Lark backlinks, appends
a follow-up comment, writes native Issue Type + initial Issue Fields, asserts
them, and closes the test issue in cleanup.

The sandbox repository lives under `TheCloverLab` because GitHub Issue Fields
are organization-scoped and are unavailable on user-owned repositories.

FiveD migration notes live in `MIGRATION_PLAN.md`. The migration path keeps the
current `~/clover/fived` bug pipeline as the only production writer until
`bugpatrol` has passed shadow reads, sandbox media e2e, and one controlled FiveD
pilot topic.

Backfill recent Lark messages:

```bash
# Check GitHub Issue Types, Issue Fields, and optional Lark access.
python -m bugpatrol doctor projects/todo-sandbox.toml
BUGPATROL_TODO_LARK_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_aac97d050d385ee9.secret)" \
python -m bugpatrol doctor projects/todo-sandbox.toml --with-lark

# Dry-run is the default and performs no GitHub writes.
BUGPATROL_TODO_LARK_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_aac97d050d385ee9.secret)" \
python -m bugpatrol backfill-lark projects/todo-sandbox.toml --limit 20

# Explicit write mode.
BUGPATROL_TODO_LARK_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_aac97d050d385ee9.secret)" \
python -m bugpatrol backfill-lark projects/todo-sandbox.toml --limit 20 --write

# Download Lark image/file/video resources to a local directory before writing
# issues. This is for local debugging and leaves local file paths in the issue.
BUGPATROL_TODO_LARK_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_aac97d050d385ee9.secret)" \
python -m bugpatrol backfill-lark projects/todo-sandbox.toml \
  --limit 20 \
  --write \
  --resource-dir .bugpatrol/resources

# Upload resources to the configured durable assets repo before writing issues.
# FiveD uses TheCloverLab/fived-assets and stores raw GitHub URLs in issues.
# The sandbox config also uses TheCloverLab/fived-assets for attachment smoke tests.
FIVED_LARK_BUG_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_a9518095a9b8ded3.secret)" \
python -m bugpatrol backfill-lark projects/fived.toml \
  --limit 20 \
  --write \
  --asset-repo

# Poll once, useful for smoke tests and cron/systemd wrappers.
BUGPATROL_TODO_LARK_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_aac97d050d385ee9.secret)" \
python -m bugpatrol watch-lark projects/todo-sandbox.toml --once --dry-run --limit 20

# Long-running polling watcher. Use --dry-run until doctor passes.
BUGPATROL_TODO_LARK_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_aac97d050d385ee9.secret)" \
python -m bugpatrol watch-lark projects/todo-sandbox.toml --interval 30 --limit 20

# Resolve owners from CODEOWNERS.
python -m bugpatrol resolve-owner ../bugpatrol-todo-sandbox src/todo/list.ts

# Search local PRD markdown cache.
python -m bugpatrol search-prd ../bugpatrol-todo-sandbox/docs/prd "todo empty state"

# Build deterministic triage context for an issue.
python -m bugpatrol issue-context projects/todo-sandbox.toml \
  --issue 3 \
  --repo-path ../bugpatrol-todo-sandbox \
  --output triage-context.md

# Validate and apply a triage JSON result.
python -m bugpatrol apply-triage-result projects/todo-sandbox.toml \
  --issue 4 \
  --input triage-output.json

# Prepare triage context/schema/agent command without executing the agent.
python -m bugpatrol run-triage projects/todo-sandbox.toml \
  --issue 3 \
  --repo-path ../bugpatrol-todo-sandbox \
  --output-dir .bugpatrol/triage-3

# Dry-run a fix notification for a PR that references exactly one issue.
python -m bugpatrol notify-fix projects/todo-sandbox.toml \
  --event pr_opened \
  --pr 12

# Write the fix notification to Lark and record BUGPATROL_FIX_META.
BUGPATROL_TODO_LARK_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_aac97d050d385ee9.secret)" \
python -m bugpatrol notify-fix projects/todo-sandbox.toml \
  --event pr_merged \
  --pr 12 \
  --write
```

`notify-fix` can infer `--issue` for `pr_opened` and `pr_merged` when the PR
references exactly one issue through GitHub closing references, `#123`, or
`owner/repo#123`. Pass `--issue` explicitly when a PR references zero or
multiple issues. A reusable GitHub Actions template is available at
`examples/github-actions/fix-notify.yml`; it defaults to dry-run until
`BUGPATROL_WRITE_FIX_NOTIFICATIONS` is set to `1`.

Opt-in live asset e2e:

```bash
BUGPATROL_LIVE_E2E=1 \
BUGPATROL_LIVE_ASSET_E2E=1 \
BUGPATROL_TODO_LARK_APP_SECRET="$(cat ~/.bugpatrol/lark/cli_aac97d050d385ee9.secret)" \
python -m unittest tests.e2e.test_live_asset_resource_loop
```

This creates a sandbox issue from a text seed, replies with a real test image in
the Lark topic, downloads the Lark resource, pushes it to
`TheCloverLab/fived-assets`, reads it back through the GitHub contents API,
appends the asset URL to the issue comment, closes the sandbox issue, and
removes the test asset file from the assets repo.

Add `BUGPATROL_LIVE_VIDEO_E2E=1` to also send a generated mp4 video reply via
`lark-cli`, download the Lark media resource, run the configured vision command,
upload the mp4 to `TheCloverLab/fived-assets`, and verify the generated
description appears in the issue comment. The video test temporarily rewrites
and restores `~/.lark-cli/config.json` so it can send with the sandbox bot.

Triage context includes the issue body, issue comments, and a `Media Evidence`
section extracted from image/video attachment lines and generated descriptions.
The default media command is `python3 -m bugpatrol.media_vision`, an
OpenAI-compatible image/video describer configured by `~/.bugpatrol/vision.json`,
`BUGPATROL_VISION_*` environment variables, or the existing
`~/.lark-bug-watcher/vision.json` fallback.
