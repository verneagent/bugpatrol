# bugpatrol plan

## Product intent

`bugpatrol` is a project-neutral bug intake and triage platform.

It should replace prompt-driven bug pipelines with deterministic services and
thin, auditable agent calls. The core rule is separation of concerns:

- Intake records what the reporter said.
- Triage decides what it means.
- Field writers update GitHub deterministically.
- Lark replies are generated from validated state, not from hidden prompt flow.
- Fixing is out of scope for now.

## Design principles

1. **GitHub is the workflow surface.** Issues, native Issue Type, Issue Fields,
   comments, assignees, and Actions are the durable state. Hidden JSON is only
   for machine backlinks and idempotency keys.
2. **Lark watcher does not triage.** It creates or updates one GitHub issue per
   Lark topic. It may use DeepSeek for title drafts, summaries, or media
   descriptions, but those are lossy helpers, not business decisions.
3. **Triage agent is pluggable.** Codex is the first target because the runner
   can use subscription auth and read the repo/PRD context. Claude must remain a
   supported provider boundary.
4. **Project config maps names, not facts.** GitHub live Issue Fields define the
   available options. `projects/*.toml` maps bugpatrol logical fields to the
   project field names and records project endpoints.
5. **Issue copy language is project config.** Intake issue body/comment copy is
   selected by `[intake].language`, currently `zh-CN` or `en-US`. The sandbox
   project uses Chinese.
6. **Ownership comes from source-of-truth files.** Use CODEOWNERS and the files
   implicated by triage. Lark @mentions are hints. Git history/blame is
   responsibility evidence, not the current owner by itself.
7. **Capability is a triage result.** It is inferred from report content, PRD
   hits, and code paths. It is validated against GitHub Issue Field options.
8. **Every external write is script-owned.** Agents output JSON. Scripts write
   GitHub fields, comments, assignees, and Lark replies.

## Current implementation

Implemented:

- Python package skeleton with no runtime third-party dependency.
- Project config loader.
- Canonical triage field specs and JSON schema.
- Intake issue body renderer with `BUGPATROL_INTAKE_META`.
- Intake workflow that creates one GitHub issue per Lark topic root, appends
  follow-up topic messages as comments, initializes deterministic intake fields,
  and replies to Lark with a GitHub backlink.
- Triage agent command abstraction with `codex` and `claude` provider branches.
- `projects/fived.toml` as a config example.
- Reusable fake GitHub/Lark clients for product-loop tests.
- Unit tests for config, field schema, intake rendering, intake workflow, and
  agent invocation.
- Local e2e fixture for Lark topic -> GitHub issue -> Lark backlink -> same
  topic follow-up -> GitHub comment.
- Real GitHub CLI issue client.
- Real Lark OpenAPI messenger client.
- Opt-in live e2e against the sandbox repo/group.

Not implemented:

- GitHub App authentication and GitHub API client.
- GitHub native Issue Type writer.
- GitHub Issue Fields reader/writer.
- Lark watcher and backfill scanner.
- Lark attachment download and asset storage.
- Lark @mention reply service.
- DeepSeek intake helper.
- PRD cache/index sync.
- CODEOWNERS parser and owner resolver.
- GitHub Actions triage workflow.
- Actual Codex/Claude execution and result ingestion.
- Deep live e2e assertions for native Issue Type and Issue Fields; blocked on
  the dedicated field writer.

## Target architecture

```text
Lark test group
  -> intake watcher
  -> GitHub issue in sandbox project
  -> GitHub Actions on self-hosted runner
  -> triage agent provider (codex or claude)
  -> validated JSON
  -> GitHub native type + Issue Fields + comment + assignee
  -> Lark follow-up / result reply
```

## Test environment

Do not test against Five Degrees production repositories or the real bug topic
group. Create a sandbox project that is similar enough to exercise the same
mechanics without touching real work.

### GitHub sandbox repo

Created repository:

- Owner: `verneagent` unless otherwise specified
- Name: `bugpatrol-todo-sandbox`
- Visibility: private
- Purpose: realistic but small ToDo app used to test bugpatrol intake and triage

The sandbox should contain:

```text
bugpatrol-todo-sandbox
├── AGENTS.md
├── .github
│   ├── CODEOWNERS
│   └── workflows
│       └── issue-triage.yml
├── docs
│   └── prd
│       ├── todo-list.md
│       ├── todo-detail.md
│       └── notifications.md
├── src
│   ├── todo
│   │   ├── model.ts
│   │   ├── list.ts
│   │   └── detail.ts
│   └── notifications
│       └── reminders.ts
└── tests
    └── todo.test.ts
```

The app does not need to ship. It only needs realistic structure:

- a PRD-like docs tree;
- CODEOWNERS with at least 2 owners;
- a few intentionally simple bugs for triage;
- GitHub Issue Types enabled: `Bug`, `Feature`, `Task`;
- Issue Fields matching bugpatrol's schema.

### Lark sandbox group

Create a dedicated Lark group, not the real Five Degrees bug group.

Proposed name:

- `Bugpatrol Todo Sandbox`

Requirements:

- The bug watcher bot is a member.
- At least one human tester is a member.
- The bot can read messages, reply in thread, download image/file resources, and
  @mention users by open_id.
- Group ID is recorded in `projects/todo-sandbox.toml`.

Current status: created.

- Name: `Bugpatrol Todo Sandbox`
- Chat ID: `oc_d371f022f168b567a141ced142691894`
- Bot app: `BugPatrol`
- Bot app ID: `cli_aac97d050d385ee9`
- Bot open ID: `ou_cef931fa2df05ca6f8ae80cb2f3e6094`
- Secret storage: local only, `~/.bugpatrol/lark/cli_aac97d050d385ee9.secret`.
- Publish status: versions `0.1.0` and `0.1.1` published with bot ability,
  WebSocket event mode, `im.message.receive_v1`, and BugBuster-equivalent IM
  scopes. `0.1.1` added `im:message.group_msg`, which is required for live
  group message history reads.
- Live verification: bot can send to the group and read recent group messages.
  Latest check sent `BugPatrol live test 2026-06-30T13:43:24.433Z...` and read
  it back as message `om_x100b6b0831146ca8e1826241cb7ff74`.
- Intake language: `[intake].language = "zh-CN"`, so sandbox GitHub issue body
  and follow-up comments are Chinese.

### Self-hosted runner

Use a trusted runner label distinct from fived:

```text
self-hosted, bugpatrol-sandbox-triage
```

The runner user should have:

- `codex login` completed if provider is `codex`;
- Claude login/config completed if provider is `claude`;
- access to clone `verneagent/bugpatrol-todo-sandbox`;
- no production fived credentials by default.

## Milestones

### M1: Sandbox repo and config

- Create private `verneagent/bugpatrol-todo-sandbox`.
- Add ToDo app skeleton, PRD docs, CODEOWNERS, and fixture issues.
- Add `projects/todo-sandbox.toml`.
- Add config validation for the sandbox.

### M2: GitHub field client

- Read org/repo Issue Fields and options.
- Validate live field options against `default_field_specs()`.
- Write native Issue Type.
- Write Issue Fields by field name and option name.
- Unit-test with fake GitHub responses.

### M3: Intake writer

- [x] Create issue from a normalized Lark intake record.
- [x] Deduplicate by `BUGPATROL_INTAKE_META.root_id`.
- [x] Append thread replies as issue comments.
- [x] Reply to Lark with the GitHub backlink.
- [x] Set initial fields:
  - `Source = Lark`
  - `Intake version = v2`
  - `Triage status = Pending`
  - `Evidence = ...`
- [x] Add unit and local e2e tests.

### M4: Lark sandbox integration

- [x] Create and activate dedicated `BugPatrol` Lark bot.
- [x] Add bot to `Bugpatrol Todo Sandbox`.
- [x] Verify live send and group history read.
- [x] Add real Lark OpenAPI messenger client.
- [x] Add opt-in live e2e that sends seed Lark messages and reads back Lark
  backlinks.
- [ ] Implement long-running watcher/backfill scanner.
- [ ] Normalize real Lark topic events into `IntakeRecord`.

### Live verification log

2026-06-30:

- Ran local unit tests: 23 passed.
- Ran local e2e tests: 1 passed, 1 live test skipped by default.
- Ran live sandbox e2e with `BUGPATROL_LIVE_E2E=1`: passed.
- Created real GitHub issue:
  `https://github.com/verneagent/bugpatrol-todo-sandbox/issues/1`
- Issue was automatically closed by cleanup.
- Verified GitHub issue body contained `BUGPATROL_INTAKE_META`.
- Verified follow-up GitHub comment contained `BUGPATROL_INTAKE_REPLY_META`.
- Verified Lark group history contained:
  - `已创建 GitHub issue #1: https://github.com/verneagent/bugpatrol-todo-sandbox/issues/1`
  - `已追加到 GitHub issue #1: https://github.com/verneagent/bugpatrol-todo-sandbox/issues/1`
- Re-ran live e2e after adding intake language config.
- Created and cleaned up issue:
  `https://github.com/verneagent/bugpatrol-todo-sandbox/issues/2`
- Verified issue #2 body uses Chinese headings:
  `## Lark 上报`, `## 原始消息`, `## 附件`.
- Verified issue #2 follow-up comment uses Chinese headings:
  `## Lark 话题更新`, `## 消息`, `## 附件`.

- Create/read Lark sandbox group config.
- Watch or scan test group.
- Download screenshots/files.
- Reply with issue link.
- Verify @mention behavior in the sandbox group.

### M5: Triage runner

- Build issue context bundle:
  - issue body/comments;
  - PRD docs;
  - CODEOWNERS;
  - relevant file tree snippets.
- Run configured provider (`codex` first, `claude` second).
- Validate JSON against schema.
- Write fields/comment/assignee.

### M6: End-to-end sandbox

- Post a realistic bug in Lark sandbox group.
- Confirm issue created in sandbox repo.
- Confirm triage workflow runs on sandbox runner.
- Confirm fields are written.
- Confirm Lark receives concise Chinese reply.
- Confirm re-triage on new Lark thread reply.

## Open decisions

- Confirm GitHub owner/name for the sandbox repo.
- Confirm who should be invited to the Lark sandbox group.
- Decide whether Issue Fields are org-level shared fields or repo-specific
  project fields for the sandbox.
- Decide whether the first triage provider is `codex` or `claude`.
- Decide where PRD cache lives for non-Lark wiki sandbox docs.
