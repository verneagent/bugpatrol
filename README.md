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
