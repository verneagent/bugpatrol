# bugpatrol — Plan

> Bug intake agent. Owns the user-facing conversation surface (Lark today, email tomorrow). Reads images and video. Creates and updates GitHub issues. **Never analyzes root cause. Never spawns fixes.**

## Background

### Why this exists

The fived repo (`/Users/dinghaozeng/clover/fived`) ships an end-to-end bug pipeline as a Claude Code skill: `/Users/dinghaozeng/clover/fived/.claude/skills/bugpipeline/SKILL.md`. That skill chains three skills — `larkbug` (intake), `ghissue triage` (analysis), and a fix loop — and runs them via the Claude Code agent CLI. It works, but:

- **Skill chaining is non-deterministic**: each step is an LLM "follow this prompt" call. The harness (Claude Code) is heavyweight, hard to test in isolation, and changes from under us when the CLI updates.
- **State is implicit and split**: lifecycle state lives across Lark reactions, GH labels, GH comment markers, and a few helper scripts (`larkbug_dedup.py`, `issue_state.py`). The skill knits them together in prose.
- **The pipeline mixes user-facing voice with engineer-facing analysis**: one agent handles "ask the reporter for a screenshot" and "git blame to find the owner". Different tool budgets, different failure modes, different audiences.

bugpatrol + bugtriage replace parts 1–2 of that pipeline with **two standalone Python apps**, each calling the [Claude Agent SDK](https://docs.anthropic.com/en/api/claude-agent-sdk) directly (not via the Claude Code CLI). The split: bugpatrol owns the reporter conversation; bugtriage owns the analysis. They communicate **only through the GitHub issue** (HTML comment markers + label).

### Why two repos

Hard separation of capabilities is cheaper than a config flag:

- bugpatrol's tool whitelist forbids `Edit`/`Write`/`Agent` and never reads source code.
- bugtriage's tool whitelist forbids `Edit`/`Write`/`Agent`/`Skill`/`Task` and never imports `lark-oapi`.
- A code review on either repo can mechanically reject "wait, why does the Lark intake agent have `git blame` access?" by pointing at the wrong import.

Local layout: `/Users/dinghaozeng/code/verneagent/{nemo,bugpatrol,bugtriage}` — three sibling repos, all under the `verneagent` GitHub org, all public.

### Scope vs. fived's existing pipeline

| Concern | bugpatrol | bugtriage | fived `/bugpipeline` (legacy) |
|---|---|---|---|
| Subscribe to Lark | yes | no | yes (`scripts/lark-bug-watcher.py`) |
| Read screenshots/video | yes | no | yes (skill: larkbug) |
| Create GH issues | yes | no | yes (skill: larkbug) |
| Mirror GH lifecycle to Lark | yes | no | yes (skill: bugpipeline mirror loop) |
| Triage / root cause / blame | no | yes | yes (skill: ghissue triage) |
| Assign owner | no | yes | yes (skill: ghissue triage) |
| Spawn fix agent | no | no | yes (skill: bugpipeline dispatch) |

The fix-spawn step (part 3) stays in fived for now. bugtriage just leaves a `<!-- TRIAGE_V1 -->` comment; whoever wants to act on it does so externally.

## Mission

One sentence: **be the only voice the bug reporter hears, and keep the GH issue body/comments faithful to whatever the reporter said.**

## In scope

- Subscribe to a Lark topic group (chat_id passed via CLI flag).
- Classify each new top-level message as: `bug` / `enhancement` / `prd-level` / `insufficient-info` / `not-actionable`.
- Read screenshots (Claude SDK multimodal) and video clips (Gemini, optional).
- Ask one targeted clarifying question when info is insufficient (max 2 rounds).
- Create GitHub issues with the labels described in [Issue labels](#issue-labels). Embed `<!-- LARK_META -->`.
- Watch the same Lark thread for follow-up replies; sync substantive replies into the GH issue as comments tagged `<!-- LARK_SYNC -->`.
- Watch GH for issue lifecycle changes (triage done, issue closed) on issues we created; mirror them back to the Lark thread.
- Mirror new human comments on GH back to the Lark thread (so the reporter sees engineer questions).
- Respond when the reporter asks "修了吗 / 修了没"-style questions by reading the GH state, not by re-asking.

## Out of scope (hard line)

- No root cause analysis. No `git blame`. No reading source.
- No spawning fix agents. No worktrees. No PR creation.
- No deciding ownership / assignee.
- No editing source files. **Tool whitelist for any LLM call MUST exclude `Edit`, `Write`, `Agent`.**

## Reference materials

These are the inputs an implementer should read before writing code. Do not start with the empty editor — most of this design is a re-shape of working code.

### Reference implementation (Claude Agent SDK usage pattern)

- **`/Users/dinghaozeng/code/verneagent/nemo`** — sibling repo. The closest "how to call claude-agent-sdk from a strongly-typed Python app" example we have. Read in this order:
  - `nemo/pyproject.toml` — Python version, mypy/ruff config, deps.
  - `nemo/nemo/claude_agent.py` — `ClaudeSDKClient` + `ClaudeAgentOptions` invocation, `allowed_tools`, `permission_mode="bypassPermissions"`, `max_buffer_size`.
  - `nemo/nemo/turn.py`, `nemo/nemo/sdk_thread.py` — anyio task lifecycle. bugpatrol's daemon does NOT need this complexity (no resumable sessions); skim only for SDK invocation patterns.

### fived skills (the legacy pipeline being replaced)

- `/Users/dinghaozeng/clover/fived/.claude/skills/bugpipeline/SKILL.md` — orchestrator. Documents the three monitors (`lark-bug-watcher.py`, `issue-watcher.sh`, `pr-review-watcher.sh`) and the cutover gate.
- `/Users/dinghaozeng/clover/fived/.claude/skills/larkbug/SKILL.md` — intake skill. Documents image embedding via the `lark-issue-assets` branch, dedup via `larkbug_dedup.py`, state via `issue_state.py`.
- `/Users/dinghaozeng/clover/fived/.claude/skills/ghissue/SKILL.md` (T1–T6 triage section) — analysis skill. bugtriage replaces this; bugpatrol only consumes its output marker.

### fived scripts to port (deterministic logic)

Don't shell out to fived — bugpatrol shouldn't depend on a sibling checkout. Port the logic in-process. Each script keeps its CLI semantics so existing fixtures still apply.

| Source path (in fived) | Target module (in bugpatrol) | Notes |
|---|---|---|
| `scripts/lark-bug-watcher.py` | `bugpatrol/sources/lark_ws.py` | WS subscriber via `lark-oapi`. |
| `scripts/lark_scan.py` | `bugpatrol/sources/lark_poll.py` | Backfill + fallback poller. |
| `scripts/lark_bot.py` | `bugpatrol/sinks/lark.py` | send/reply/react/list-messages. |
| `scripts/larkbug_dedup.py` | `bugpatrol/state/dedup.py` | Search GH issues by `LARK_META.root_id`. |
| `scripts/issue_state.py` (reconcile path) | `bugpatrol/state/reconcile.py` + `state/derive.py` | Pure-derive. No persisted enum. |
| `scripts/followups_scan.py` | folded into `pipelines/collect.py` poll loop | |
| `scripts/pr_linked_issue.py` | folded into `state/derive.py` | We only need open/closed; PR linking is incidental. |
| `scripts/gh-as-bot.sh` | vendored verbatim into `bugpatrol/scripts/gh-as-bot.sh` | sobit-bot token wrapper; required for any GH write. |

`scripts/dispatch_pending_fixes.py`, `scripts/triage_backlog.py`, `scripts/bugpipeline-doctor.sh` stay in fived — they belong to the fix loop or are pipeline-wide.

### External APIs / SDKs

- **Claude Agent SDK (Python)** — `claude-agent-sdk` on PyPI. Docs: https://docs.anthropic.com/en/api/claude-agent-sdk. Used for every LLM step.
- **`lark-oapi`** — Python SDK for Lark. WS client + IM v1 API. Already used in fived; port the same client wiring.
- **Gemini Python SDK** — `google-genai`. Used only for `describe_video` (M6).
- **`gh` CLI** — wrapped through `scripts/gh-as-bot.sh` for writes; plain `gh` for reads is fine in dev but production runs always go through the wrapper.

### Identity / secrets

- **Lark app**: same `bugpatrol` Lark app already configured in fived. Secret file path is environment-dependent — load via config, not hardcoded.
- **GH App**: same `sobit-bot` GitHub App used by fived (default per the [Open questions](#open-questions) section). `gh-as-bot.sh _token` prints a fresh installation token.

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

## Issue labels

Kept deliberately small. Every issue bugpatrol creates carries `bugpatrol` plus a type and (for bugs) a priority. State is derived from observable artifacts (issue open/closed, presence of TRIAGE_V1, etc.), not from labels.

| Label | Set by | Meaning |
|---|---|---|
| `bugpatrol` | bugpatrol at create | "this issue is owned by the bugpatrol/bugtriage pipeline" — cutover gate, never removed |
| `bug` or `enhancement` | bugpatrol at create | category from classifier (mutually exclusive) |
| `P0-critical` … `P3-low` | bugpatrol at create | priority from classifier (bugs only) |
| `needs-confirmation` | bugpatrol at create when `ask_count >= 2` | reporter never produced enough info; created defensively |

bugpatrol **never** removes the `bugpatrol` label. If it goes missing, treat as data loss and refuse to mirror or modify the issue.

No `lark-reported`, `triaged`, `lark/needs-fix-agent`, or `lark/state:*` labels. Source channel is encoded inside `LARK_META`. Triage status is encoded as the presence of a `<!-- TRIAGE_V1 -->` comment. Fix dispatch is out of scope.

## Conversation state machines

Two independent state machines, both **derived** every loop tick from observable storage. No persisted enum.

### Lark topic states (4)

Per Lark thread (root message + replies):

| State | Predicate | Meaning |
|---|---|---|
| `NEW` | no `:eyes:` reaction by self on root | not yet seen |
| `IGNORED` | self reacted `:eyes:`, no GH issue created, no clarifying question asked | bot looked, decided no action (not-actionable / prd-level / off-topic) |
| `WAITING` | self asked a clarifying question, no reply yet | needs reporter follow-up; `ask_count` tracked via self-reaction count |
| `LINKED` | a GH issue exists with `LARK_META.root_id == thread.root_id` | issue created; further updates flow through mirror loop |

Transitions: `NEW → IGNORED` (terminal until new reply), `NEW → WAITING`, `NEW → LINKED`, `WAITING → LINKED`, `WAITING → IGNORED`, `LINKED → LINKED` (sync follow-up replies into the issue). Any new non-self reply on an `IGNORED` thread bumps it back to `NEW` for re-evaluation.

### GH issue states (3)

Per GH issue carrying `LARK_META`:

| State | Predicate | Meaning |
|---|---|---|
| `REPORTED` | issue is open, no `<!-- TRIAGE_V1 -->` comment yet | bug filed, awaiting bugtriage |
| `TRIAGED` | issue is open, has at least one `<!-- TRIAGE_V1 -->` comment | bugtriage has spoken (any verdict) |
| `CLOSED` | issue is closed (regardless of triage) | nothing more to mirror until reopened |

Mirror loop emits one `LARK_LIFECYCLE` notification per state transition (`reported`, `triaged`, `closed`), recorded in the `LARK_STATE_V1` pinned comment for idempotency.

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

Every poll tick (default 5 min), for each issue carrying `LARK_META` (open OR recently closed):

1. **Lifecycle delta**: derive current GH issue state — `REPORTED` (open, no TRIAGE_V1), `TRIAGED` (open, has TRIAGE_V1), or `CLOSED` (closed). Compare against `LARK_STATE_V1` comment (parsed). For each stage not yet announced (`reported` / `triaged` / `closed`), post to Lark thread + record in state comment.
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
      derive.py               # GH state → enum (REPORTED/TRIAGED/CLOSED); Lark thread → enum (NEW/IGNORED/WAITING/LINKED)
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
| "asked a clarifying question already?" | `:question:` reaction by self on the message (count = `ask_count`) |
| "this issue belongs to which Lark thread?" | `LARK_META` HTML comment in issue body |
| "what lifecycle notifications were sent for this issue?" | `LARK_STATE_V1` JSON in pinned bot comment (`reported` / `triaged` / `closed`) |
| "does a GH issue already exist for this thread?" | live `gh issue list` filtered by `LARK_META.root_id` |
| "last human reply we mirrored?" | newest `LARK_LIFECYCLE: stage=mirror_comment` in thread |

Restart → re-derive everything from GH + Lark.

## Execution playbook

Each step is independently shippable and ends with a concrete verification command. Do not start step N+1 until step N's verification passes against a real Lark + GH target.

### M0: Bootstrap (no LLM, no Lark)

**Deliverable**: empty package that lints, type-checks, and runs `bugpatrol --help`.

1. `cd /Users/dinghaozeng/code/verneagent/bugpatrol` (already cloned).
2. Write `pyproject.toml` modeled after `nemo/pyproject.toml`:
   - `requires-python = ">=3.12"` (3.14 if available, matching nemo).
   - Deps: `pydantic>=2.7`, `claude-agent-sdk`, `lark-oapi`, `typer` (or `click`), `anyio`, `httpx`.
   - Dev deps: `mypy`, `ruff`, `pytest`, `pytest-asyncio`.
   - `[tool.mypy] strict = true`.
   - `[tool.ruff] target-version = "py312"` (or py314).
3. Create the directory tree under `bugpatrol/bugpatrol/` exactly per [Module layout](#module-layout-proposed). Every leaf is an empty `__init__.py` plus a stub module file with a `# TODO(M<n>)` comment naming the milestone that fills it.
4. `bugpatrol/cli.py` defines a Typer app with placeholder subcommands matching the [CLI](#cli) section. Each prints `NotImplementedError` and exits 2.
5. `bugpatrol/__main__.py` calls `cli.app()`.
6. `bugpatrol/config.py` defines `BotIdentity` (per the [Bot identity](#bot-identity--self-detection) section) and a `Config` BaseModel that loads from TOML via `tomllib`. **No defaults for secrets.**
7. Vendor `gh-as-bot.sh` from fived: copy `/Users/dinghaozeng/clover/fived/scripts/gh-as-bot.sh` to `bugpatrol/scripts/gh-as-bot.sh`, mark executable, add `chmod +x` to a `Makefile` install target.
8. CI: GitHub Actions workflow that runs `ruff check`, `ruff format --check`, `mypy bugpatrol`, `pytest`.

**Verify**:
```
uv run bugpatrol --help     # or `python -m bugpatrol --help`
ruff check . && mypy bugpatrol && pytest -q
```
Both must exit 0. No network, no LLM call.

### M1: `bugpatrol process <msg_id>` — single-shot collect

**Deliverable**: given one Lark message_id on a configured chat_id, create exactly one GH issue and one Lark thread reply.

Files to write (in order):

1. `sinks/lark.py` — port `lark_bot.py`. Methods: `get_message(msg_id)`, `list_thread_replies(root_id)`, `reply_in_thread(root_id, text)`, `add_reaction(msg_id, emoji)`, `download_image(image_key) -> Path`.
2. `sinks/gh.py` — wrap `scripts/gh-as-bot.sh`. Methods: `create_issue(title, body, labels) -> issue_number`, `add_comment(issue, body)`, `view_issue(n) -> dict`, `list_issues(query) -> list[dict]`. All reads via `gh issue/api`; all writes via the wrapper.
3. `state/meta.py` — parsers/writers for `LARK_META`, `LARK_SYNC`, `LARK_LIFECYCLE`, `LARK_STATE_V1`. Pure-function module; round-trip JSON in HTML comments.
4. `state/dedup.py` — port `larkbug_dedup.py`. `find_issue_for_root(root_id) -> int | None` via `gh issue list -S 'LARK_META...root_id...'`.
5. `media/images.py` — download Lark image, shrink via `sips`, push to a per-issue branch named `lark-issue-assets/<issue_num>` (mirror fived's behavior — see `larkbug` skill section "Embed").
6. `schemas.py` — every pydantic model used so far: `BotIdentity`, `Config`, `LarkMessage`, `IssueRef`, `MessageClassification`, `ScreenshotDescription`, `LarkMeta`.
7. `sdk.py` — one-shot Claude Agent SDK runner. Signature: `async def run(prompt: str, *, model: str, allowed_tools: list[str], system_prompt: str, response_model: type[T]) -> T`. Internals follow `nemo/nemo/claude_agent.py`: fresh `ClaudeSDKClient`, `permission_mode="bypassPermissions"`, drain messages, parse JSON block matching `response_model`. Hard-fail on non-JSON or schema mismatch.
8. `prompts/classify.md`, `prompts/describe_screenshot.md` — version-controlled, with examples. Render via `string.Template` in `sdk.py`.
9. `pipelines/collect.py`:
   ```python
   async def collect_one(msg_id: str, cfg: Config) -> CollectOutcome:
       msg = await lark.get_message(msg_id)
       if not classify(msg).is_actionable:
           await lark.add_reaction(msg_id, "eyes")
           return CollectOutcome.IGNORED
       if existing := dedup.find_issue_for_root(msg.root_id):
           return CollectOutcome.ALREADY_LINKED(existing)
       desc = await describe_attachments(msg)
       title, body = render_issue(msg, desc)
       num = await gh.create_issue(title, body, labels=[...])
       await lark.reply_in_thread(msg.root_id, f"已创建 issue #{num}")
       await lark.add_reaction(msg_id, "white_check_mark")
       return CollectOutcome.CREATED(num)
   ```
10. `cli.py` — wire up `bugpatrol process`.

Tests:
- `tests/fixtures/lark_bug_with_screenshot.json` — captured Lark message payload.
- `tests/fixtures/gh_issue_create_response.json` — expected GH issue body shape.
- `tests/test_classify.py` — stub `sdk.run` to return canned `MessageClassification`.
- `tests/test_collect_e2e.py` — stub Lark + GH sinks; assert one create call, one reply, two reactions.

**Verify**:
```
bugpatrol process --chat-id oc_test --config config.toml <real_msg_id>
gh issue view <created_issue> --json body,labels,comments
```
The issue body must contain `<!-- LARK_META: ... -->`. Labels must be exactly `bugpatrol` + `bug|enhancement` + `P*-*`. The Lark message must have `:eyes:` and `:white_check_mark:` reactions.

### M2: `bugpatrol scan` — backfill loop

**Deliverable**: `bugpatrol scan --chat-id ... [--hours 4]` walks recent messages and processes each with `collect_one`, idempotent.

Files:
1. `sources/lark_poll.py` — port `lark_scan.py`. `iter_recent_root_messages(chat_id, since: datetime) -> AsyncIterator[LarkMessage]`. Uses `im.v1.message.list`.
2. Extend `pipelines/collect.py` with `should_process(msg) -> bool`: trigger rule from [Detection rules](#detection-rules).

Tests:
- `tests/test_scan_idempotent.py` — run scan twice over the same fixture, assert second run makes zero writes.

**Verify**: run scan twice in production; second run logs "0 new" and emits no Lark/GH writes.

### M3: thread-reply handling

**Deliverable**: when a non-self reply lands on a Lark thread that already has a linked issue, sync that reply into the GH issue as a `LARK_SYNC` comment.

Files:
1. Extend `pipelines/collect.py` with `sync_reply_to_issue(thread, reply, issue_num)`. Writes a comment with body:
   ```
   <!-- LARK_SYNC: msg_id=... ts=... sender=... -->
   **@<reporter>** (Lark, <ts>):
   <body>
   ```
2. `cli.py` — extend `process` to handle "this msg is a reply, not a root".

Tests:
- `tests/test_thread_reply.py` — fixture: thread with prior issue, new non-self reply. Assert `LARK_SYNC` comment created, no duplicate issue.

**Verify**: post a follow-up Lark reply to a known thread; observe one `LARK_SYNC` comment on the linked issue, no second issue created, `:eyes:` added to the reply.

### M4: `bugpatrol mirror` — GH→Lark sync

**Deliverable**: one-shot `bugpatrol mirror [--issue N | --all]` mirrors lifecycle changes and engineer comments back to Lark.

Files:
1. `state/derive.py` — `derive_gh_state(issue_json) -> Literal["REPORTED","TRIAGED","CLOSED"]`. Pure function, exhaustive `match`.
2. `state/reconcile.py` — port `issue_state.py reconcile`. Reads/writes the `LARK_STATE_V1` pinned comment.
3. `pipelines/mirror.py` — main loop:
   ```python
   async def mirror_one(issue_num: int, cfg: Config):
       issue = await gh.view_issue(issue_num)
       if not (meta := parse_lark_meta(issue.body)):
           return
       state = derive_gh_state(issue)
       sent = parse_lark_state(issue.comments)
       for stage in unsent_stages(state, sent):
           await lark.reply_in_thread(meta.root_id, render_lifecycle(stage, issue))
           await gh.add_comment(issue_num, render_lark_lifecycle_marker(stage))
       for c in new_comments_since_last_mirror(issue, sent):
           if marker(c) in ("LARK_SYNC", "LARK_LIFECYCLE", "LARK_STATE_V1"):
               continue
           if marker(c) == "TRIAGE_V1":
               await mirror_triage(meta, c)
           else:
               await mirror_engineer(meta, c)
   ```
4. `prompts/summarize_triage.md` — for `summarize_triage_for_lark`. Input: parsed TRIAGE_V1 JSON. Output: short Chinese summary + verdict label.

Tests:
- `tests/test_mirror_idempotent.py` — fixture: open issue with TRIAGE_V1, no prior LARK_STATE_V1. First mirror sends `reported` + `triaged`; second sends nothing.

**Verify**: trigger triage on a known issue; run `bugpatrol mirror --issue N`; observe two Lark replies (`reported`, `triaged`); re-run; observe zero new replies.

### M5: `bugpatrol watch` — daemon

**Deliverable**: long-running daemon that combines WS event handling + two poll loops.

Files:
1. `sources/lark_ws.py` — port `lark-bug-watcher.py`. WS client emits `LarkMessage` events into an `anyio.MemoryObjectSendStream`.
2. `daemon.py`:
   ```python
   async def run(cfg: Config):
       async with anyio.create_task_group() as tg:
           tg.start_soon(ws_loop, cfg)
           tg.start_soon(lark_poll_loop, cfg, interval=300)
           tg.start_soon(mirror_poll_loop, cfg, interval=300)
   ```
3. Concurrency: each loop uses `anyio.Semaphore(cfg.max_concurrent)` around `collect_one`/`mirror_one`.
4. Signal handling: SIGINT/SIGTERM → cancel scope, drain in-flight tasks, exit 0.

Tests:
- `tests/test_daemon_lifecycle.py` — start daemon, send fake WS event, assert collect_one invoked, send SIGINT, assert clean shutdown.

**Verify**: `bugpatrol watch --chat-id ...`; post a Lark message; observe issue created within ~10s. `kill -TERM <pid>`; observe graceful shutdown in logs.

### M6: video understanding (Gemini)

**Deliverable**: `--video-provider gemini` accepts video attachments on Lark messages and treats them as another `describe_*` source.

Files:
1. `media/video.py` — Gemini client. Input: video bytes + prompt. Output: `VideoDescription` (same shape as `ScreenshotDescription` plus `timestamps[]`).
2. Extend `pipelines/collect.py` `describe_attachments` to dispatch on MIME type.

**Verify**: post a Lark message with an MP4; observe issue body contains a "Video" section with frame timestamps.

### M7: clarification rounds

**Deliverable**: when classifier returns `insufficient-info`, ask one targeted question; track ask-count via `:question:` reactions; on second insufficient pass, create the issue with `needs-confirmation` label.

Files:
1. `prompts/clarify.md` — composer prompt.
2. `pipelines/collect.py` — branch on classifier output; `ask_count = count_self_reaction(msg, "question")`.

**Verify**: post a vague Lark message; observe one Lark reply asking for steps; reply with garbage; observe issue created with `needs-confirmation`.

### M8: email source

**Deliverable**: `sources/email.py` adapter. Out of scope for first release; design ensures `pipelines/collect.py` is source-agnostic from M1.

---

**Rule**: do not start step N+1 until step N's verify command passes in production. Do not add cross-cutting "improvements" mid-step.

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
