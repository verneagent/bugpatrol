# bugpatrol — Plan

> Bug intake agent. Owns the user-facing conversation surface (Lark today, email tomorrow). Reads images and video. Creates and updates GitHub issues. **Never analyzes root cause. Never spawns fixes.**

## Mission

One sentence: **be the only voice the bug reporter hears, and keep the GH issue body/comments faithful to whatever the reporter said.**

## In scope

- Subscribe to a Lark topic group (chat_id passed via CLI flag).
- Classify each new top-level message as: `bug` / `enhancement` / `prd-level` / `insufficient-info` / `not-actionable`.
- Read screenshots (Claude SDK multimodal) and video clips (Gemini, optional).
- Ask one targeted clarifying question when info is insufficient (max 2 rounds).
- Create GitHub issues with `bug` or `enhancement` label. Embed `<!-- LARK_META -->`.
- Watch the same Lark thread for follow-up replies; sync substantive replies into the GH issue as comments tagged `<!-- LARK_SYNC -->`.
- Watch GH for issue lifecycle changes (triage, PR opened, PR merged) on issues we created; mirror them back to the Lark thread.
- Mirror new human comments on GH back to the Lark thread (so the reporter sees engineer questions).
- Respond when the reporter asks "修了吗 / 修了没"-style questions by reading the GH state, not by re-asking.

## Out of scope (hard line)

- No root cause analysis. No `git blame`. No reading source.
- No spawning fix agents. No worktrees. No PR creation.
- No deciding ownership / assignee.
- No editing source files. **Tool whitelist for any LLM call MUST exclude `Edit`, `Write`, `Agent`.**

## Data sources & sinks

| Direction | Channel | Implementation |
|---|---|---|
| inbound | Lark WS | `lark-oapi` `lark.ws.Client`, port from fived's `scripts/lark-bug-watcher.py` |
| inbound | Lark backfill | `im.v1.message.list`, port from fived's `scripts/lark_scan.py` |
| inbound (future) | email | IMAP or Gmail API; same downstream pipeline |
| outbound | Lark thread reply | `im.v1.message.create` with `reply_in_thread=true` |
| outbound | Lark reaction | `im.v1.message.reactions.create` (used as watermark, see "State") |
| inbound | GitHub | `gh issue list/view --json ...` polling |
| outbound | GitHub | `gh issue create/comment/edit` via `scripts/gh-as-bot.sh` (vendored) |

## Bot identity & self-detection

Hard constants (loaded from config, not hardcoded in modules):

```python
class BotIdentity(BaseModel):
    lark_app_id: str
    lark_app_secret_path: Path
    lark_open_id: str        # for "is this message mine" checks
    gh_app_token_cmd: list[str]  # e.g. ["bash", "scripts/gh-as-bot.sh", "_token"]
```

**Lark side**: a message is "mine" iff `msg.sender.sender_id.open_id == BotIdentity.lark_open_id`. No marker needed; sender ID is authoritative.

**GH side**: a comment is "mine" iff body starts with `<!-- LARK_SYNC` or `<!-- LARK_LIFECYCLE`. The bot account is shared with bugtriage, so author check alone is insufficient — markers are the disambiguator.

| Marker | Written by | Meaning to bugtriage |
|---|---|---|
| `<!-- LARK_META: ... -->` | bugpatrol on issue body | issue originated from Lark |
| `<!-- LARK_SYNC: msg_id=... ts=... -->` | bugpatrol on issue comment | new info from reporter; treat as fresh human comment |
| `<!-- LARK_LIFECYCLE: stage=... -->` | bugpatrol on issue comment | bot housekeeping; ignore |
| `<!-- LARK_STATE_V1 -->` | bugpatrol on issue comment | state-machine record (port of `scripts/issue_state.py`); ignore |
| `<!-- TRIAGE_V1: ts=... -->` | bugtriage on issue comment | triage analysis; bugpatrol summarizes back to Lark |

## Conversation state machine (per Lark thread)

Stored entirely on GH (issue state, labels, comments) and Lark (reactions). No SQLite.

```
NEW              → has thread root, no GH issue yet
INSUFFICIENT     → issue not created, asked once, waiting
ISSUE_OPEN       → GH issue exists, label:bug or :enhancement, no triaged
TRIAGED          → issue has TRIAGE_V1 comment + label:triaged
PR_OPEN          → linked PR exists, not merged
FIXED            → linked PR merged + issue closed
REJECTED         → triage verdict was "Works as designed" / "Spec gap" → labeled accordingly
```

State is **derived**, not stored. Each loop tick re-derives state from GH + Lark API responses.

## Detection rules

### When to handle a Lark thread (WS event OR poll tick)

For each thread (group of messages sharing a `root_id`):

```
last_human   = newest message where sender_id != bot
last_self    = newest message where sender_id == bot
trigger if last_human exists AND
  (last_self is None OR last_human.ts > last_self.ts) AND
  reaction(":eyes:") not present on last_human by self
```

Reactions act as a per-message "I've seen this" watermark, scoped to the bot's own reaction set. After fully processing a message, bugpatrol adds `:eyes:` (queued for failed processing → rerun) plus `:white_check_mark:` (issue created/updated).

### When to mirror an issue back to Lark

Every poll tick (default 5 min), for each open issue carrying `LARK_META`:

1. **Lifecycle delta**: derive current state from GH labels + closing PRs. Compare against `LARK_STATE_V1` comment (parsed). For each missing notification stage, post to Lark thread + record in state comment.
2. **Comment delta**: for each issue comment created after the last `LARK_LIFECYCLE` mirror message in the thread, classify by marker:
   - `LARK_SYNC` → skip (we wrote it)
   - `LARK_LIFECYCLE` / `LARK_STATE_V1` → skip
   - `TRIAGE_V1` → summarize verdict + root cause, mirror as "Triage 完成 / 已更新"
   - anything else → human engineer comment, mirror verbatim with `Engineer @<author>: <body>` prefix

## LLM steps

Each step is one `claude-agent-sdk` call. Fresh `ClaudeSDKClient`, no resume, no shared state, hard tool whitelist, JSON-only output validated by pydantic.

| Step | Tools | Model (default) | Output schema |
|---|---|---|---|
| `classify_message` | none | `claude-haiku-4-5` | `MessageClassification` |
| `describe_screenshot` | `Read` | `claude-sonnet-4-6` | `ScreenshotDescription` |
| `describe_video` | (Gemini, separate provider) | `gemini-2.5-flash` | `VideoDescription` (same shape as screenshot, plus `timestamps[]`) |
| `compose_clarifying_question` | none | `claude-haiku-4-5` | `{question: str, fields_missing: list[str]}` |
| `answer_thread_question` | `Bash` (read-only `gh issue/pr view`) | `claude-sonnet-4-6` | `{reply: str, citations: list[str]}` |
| `summarize_triage_for_lark` | none | `claude-haiku-4-5` | `{verdict, summary, action_required: bool}` |

**No LLM call has access to `Edit`, `Write`, `Agent`, `Skill`, or `Bash` writes.** `Bash` when allowed is restricted by prompt to `gh issue view`, `gh pr view`, `gh api -X GET ...` only — sandboxed by review at PR time, not by runtime enforcement.

## Module layout (proposed)

```
bugpatrol/
  pyproject.toml              # python>=3.12, pydantic v2, mypy strict, ruff
  bugpatrol/
    __main__.py
    cli.py                    # typer or argparse
    config.py                 # BotIdentity, paths, env
    sdk.py                    # one-shot Claude Agent SDK turn runner
    schemas.py                # pydantic models for every LLM output + every GH/Lark payload
    prompts/                  # *.md, version-controlled, unit-tested
      classify.md
      describe_screenshot.md
      describe_video.md
      clarify.md
      answer.md
      summarize_triage.md
    sources/
      lark_ws.py              # WS subscriber
      lark_poll.py            # backfill / fallback poller
      email.py                # placeholder for future
    sinks/
      lark.py                 # send/reply/react/list-messages/download-image
      gh.py                   # issue create/comment/edit/view via gh-as-bot.sh
    media/
      images.py               # download + sips shrink, embed via lark-issue-assets branch
      video.py                # Gemini provider (optional)
    state/
      derive.py               # GH state → enum (REPORTED/TRIAGED/FIXING/FIXED/...)
      reconcile.py            # port of scripts/issue_state.py reconcile-all
      meta.py                 # LARK_META / LARK_STATE_V1 / LARK_SYNC parsers + writers
      dedup.py                # port of scripts/larkbug_dedup.py
    pipelines/
      collect.py              # one Lark message → issue (or thread reply → GH comment)
      mirror.py               # one GH issue → Lark thread updates
    daemon.py                 # asyncio: WS + 2 poll loops with semaphores
  tests/
    fixtures/                 # recorded Lark payloads + GH issue JSON
    test_classify.py          # stub LLM
    test_collect_e2e.py
    test_mirror.py
```

## CLI

```
bugpatrol watch    --chat-id oc_xxx [--poll-interval 300] [--video-provider gemini]
bugpatrol scan     --chat-id oc_xxx [--hours 4]
bugpatrol process  --chat-id oc_xxx <msg_id|keyword>
bugpatrol mirror   [--issue N | --all]      # one-shot GH→Lark sync
bugpatrol doctor   [--quick] [--json]
```

All commands accept `--config /path/to/bugpatrol.toml` for non-default identity / secrets.

## State storage

**No SQLite. No jsonl. No filesystem state across restarts.**

| Concern | Where |
|---|---|
| "this Lark message processed?" | `:eyes:` reaction by self on the message |
| "this issue belongs to which Lark thread?" | `LARK_META` HTML comment in issue body |
| "what notifications were sent for this issue?" | `LARK_STATE_V1` JSON in pinned bot comment |
| "is there an open PR for this thread?" | live query via `larkbug_dedup` (search by `LARK_META.root_id`) |
| "last human reply we mirrored?" | newest `LARK_LIFECYCLE: stage=mirror_comment` in thread |

Restart → re-derive everything from GH + Lark.

## Reuse from `fived` repo

These existing scripts encapsulate deterministic logic worth porting (not shelling out — bugpatrol shouldn't depend on the fived checkout being present). Each port should keep the script's CLI semantics so bugpatrol can be unit-tested against the same fixtures the scripts already use:

- `scripts/lark-bug-watcher.py` → `sources/lark_ws.py`
- `scripts/lark_scan.py` → `sources/lark_poll.py`
- `scripts/lark_bot.py` → `sinks/lark.py`
- `scripts/larkbug_dedup.py` → `state/dedup.py`
- `scripts/issue_state.py` (reconcile path) → `state/reconcile.py` + `state/derive.py`
- `scripts/followups_scan.py` → fold into `pipelines/collect.py` polling loop
- `scripts/pr_linked_issue.py` → fold into `state/derive.py`

`scripts/dispatch_pending_fixes.py`, `triage_backlog.py`, `bugpipeline-doctor.sh` stay in fived (they belong to the fix-spawn loop or are pipeline-wide).

## Milestones

Strictly incremental — each milestone is independently shippable and verifiable:

1. **M1: `bugpatrol process <msg_id>`** — single-shot, no WS, no poll. Takes one Lark message_id, runs the full collect pipeline, exits. Side effects: one GH issue + one thread reply. **No mirror loop, no clarification rounds.** Validates: classify, screenshot describe, dedup, GH create, state seeding.
2. **M2: `bugpatrol scan`** — backfill mode. Same logic as M1 but loops over recent messages with reaction-watermark dedup. Validates: backfill semantics, idempotency.
3. **M3: thread reply handling** — extends M2 to detect non-self replies on existing threads, sync to GH issue, ack in Lark. Validates: substantive vs not, marker writes, retriggering retriage on existing issue.
4. **M4: `bugpatrol mirror`** — one-shot GH→Lark sync. Lifecycle deltas + comment mirroring. Validates: state derive, notification idempotency.
5. **M5: `bugpatrol watch`** — daemon. WS + two poll loops + semaphores + signal handling. Validates: long-running stability, recovery from WS drops.
6. **M6: video** — `--video-provider gemini`. Validates: media pipeline extension point.
7. **M7: clarification rounds** — when classify returns `insufficient-info`, ask one targeted question. Track ask-count via a self-reaction (e.g. `:question:` ×N).
8. **M8: email source** — second source. Validates: source-adapter abstraction.

Don't start M2 until M1 is in production. Don't add features mid-milestone.

## Open questions

- **Issue assets branch**: today fived's `lark-issue-assets` branch holds screenshots. bugpatrol needs to write there. Solutions: (a) bugpatrol clones fived as a working tree, (b) screenshots go to a separate verneagent-owned repo. (a) is operationally simpler (one repo, existing flow); (b) is cleaner separation. Default (a) for now.
- **Bot account**: same `sobit-bot` GitHub App as fived, or separate `bugpatrol-bot`? Same is easier to start; separate gives better marker-free attribution. Default same.
- **Video provider config**: `--gemini-key-file` vs `GEMINI_API_KEY` env? Default file (matches Lark secret pattern).
- **Concurrency**: how many concurrent collect / mirror tasks? Default 2 each. Tunable via `--max-concurrent N`.
- **Test account**: do we have a non-prod Lark topic group to point staging at, or is testing always against the live "FiveD Bug Test" group? If always live, every dev cycle either spams or relies on `--dry-run`.

## Non-goals reminder (refresh on every PR review)

If a PR for bugpatrol introduces any of these, **reject**:

- Importing anything from `claude_agent_sdk` other than `ClaudeSDKClient` + `ClaudeAgentOptions`.
- Calling an LLM with `Edit`, `Write`, `Agent`, or `Skill` in `allowed_tools`.
- Reading source files for analysis (any path under `app/`, `weaver/`, `cli/` of the fived repo).
- Writing to GitHub issues outside the `bug` / `enhancement` label space, or to repos other than the configured target.
- Spawning subprocesses other than `gh`, `git` (read), Lark API SDK calls, Gemini SDK calls.
- Adding any persistent state file under `~/.bugpatrol/` other than the config file.
