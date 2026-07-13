# Fix-run progress reporter (design)

Status: implementing 2026-07-13.

## Problem

A fix run on a heavy repo (fived: RN/Expo) is long: `npm ci`, the agent's
edit turns, and the **full** `preflight.sh` gate (type check + lint + unit +
build + surface) can total 10–30 min. Today `execute_fix_run` posts one
"开始自动修复" ping and then goes **silent** until the terminal notification
(`opened_pr` / `verify_failed` / `baseline_broken`). During that window a human
watching the bug's Lark topic can't tell a live-but-slow run from a hung/dead
one — this is the #3974 anxiety.

## Scope — this fills the *pre-PR* silence

This is distinct from and complementary to the **CI-FIX-FEEDBACK** loop:

| Loop | When | Reacts to | Emits |
| --- | --- | --- | --- |
| **progress reporter** (this) | *during* a fix run, before any PR | in-process phase boundaries + a heartbeat clock | liveness pings to the reporter's Lark topic |
| **CI-fix feedback** (`docs/CI-FIX-FEEDBACK.md`) | *after* a fix PR exists | the project's `workflow_run` conclusion | build-ready ping (success) / auto-fix revise (failure) |

They don't overlap: the reporter covers the gap the terminal notification and
the CI loop both start *after*. Sequencing: ship the **reporter first** (small,
self-contained, no external prerequisite). CI-FIX-FEEDBACK stays blocked on a
real green `bugpatrol/fix-issue-N` PR through pr-ci, which we don't have yet
(#3974 failed preflight → no PR).

## Design — one bounded heartbeat thread

- A single background **daemon thread** spans the whole `execute_fix_run`. It
  wakes every `[fix.progress].heartbeat_seconds` (default 300; `0` disables)
  and posts the **current phase + elapsed time** to the reporter's Lark topic
  (chat_id/message_id parsed once from the issue body's intake meta — no extra
  GitHub call per beat).
- `execute_fix_run` calls `reporter.set_phase(...)` at each boundary
  (agent 编辑 → 校验改动 → 跑验证门 → 提交并开 PR). The thread reports whatever
  phase is current, so a hang inside **any** phase (including the long
  `npm ci` / preflight) surfaces, not just phase transitions.
- **Bounded** by `max_beats` (default 12 → ≤ 60 min at 300s) so a hung run
  can't spam the topic forever.
- **Best-effort, never fails the run**: a withdrawn source message is swallowed
  (same tolerance as the other topic pings); any other Lark error is *logged*
  to stderr in the thread loop and the run continues (No-Silent-Failures: log,
  don't crash a best-effort background ping).
- **Stateless / cross-machine safe**: no persisted state; the reporter lives
  and dies with the one run's process. The heartbeat message-id is held
  in-memory only for the run's duration.

## Why not

- **Not a separate observer workflow** polling `gh run list`: the in-process
  reporter knows the exact phase and elapsed with zero polling/coupling; an
  external watcher can't see the phase and would die-blind to hangs anyway.
- **Not message-editing (single updating line)**: a first cut of bounded fresh
  replies is simpler, needs no new Lark API, and each ping doubles as a
  liveness + elapsed signal. Editing-in-place is a possible later UX upgrade.

## Changes (bugpatrol repo)

1. `config.py` — `[fix.progress].heartbeat_seconds` (default 300, `_num`,
   `< 0` rejected; `0` = disabled, explicit not footgun) on `FixConfig`.
2. `bugpatrol/progress.py` — `render_progress_message`, `format_elapsed`,
   `ProgressReporter` (thread-free `beat()` core + daemon-thread runner).
3. `fix_runner.py` — build the reporter from the issue's intake meta, start it
   after the "开始修复" ping, `set_phase(...)` at boundaries, stop in `finally`.
4. Tests — `test_progress.py` (render, cap, phase reflection, disabled,
   withdrawn-tolerance), `test_config.py` (new field default/reject).

## Later (not in first cut)

- Extend the reporter to `execute_fix_revise` (revise is also long).
- Message-edit-in-place for a single updating progress line.
