# bugpatrol TODO

Project-neutral product work that remains after the current intake, triage,
media, and fix-notification slices.

## Follow-Up After Triage

- Add `Needs review` to `Triage status`.
- Add a deterministic follow-up classifier:
  - acknowledgement / non-actionable
  - material new evidence
  - fix/status chatter
- When material evidence arrives after `Done` or `Skipped`, move status to
  `Needs review`.
- When material evidence arrives during `Running`, do not cancel the run; mark a
  pending review flag in metadata and prevent the final state from silently
  becoming `Done`.
- Add debounce-based optional re-triage after no new follow-up arrives for a
  configured interval.

## Watcher Robustness

- Add a durable processed-message ledger.
- Add a watcher lease/lock so one Lark group has one active writer.
- Add a second create-race check around GitHub issue creation.
- Add structured logs for scanned/skipped/processed messages.
- Add a WebSocket event watcher while keeping polling as fallback.
- Add reconnect/backoff and heartbeat handling for WebSocket mode.

## Triage Automation

- Add GitHub Actions workflow examples for `run-triage`.
- Add active-run metadata so concurrent triage jobs for the same issue do not
  overwrite each other.
- Add stale-run detection when issue comments changed after context generation.
- Add a dry-run report that shows which fields would change.
- Add owner override config for projects where CODEOWNERS is not enough.

## Media Handling

- Add configurable image resizing before vision and upload.
- Add video size limits and duration limits.
- Add video frame extraction or clipping for large videos.
- Add retry policy for transient vision API failures.
- Add media redaction hooks for sensitive screenshots.
- Add attachment cleanup/reconcile command for test assets.

## Fix Notifications

- Add GitHub Actions workflow examples for PR opened, PR merged, and issue
  closed events.
- Add scheduled reconcile for missed PR/commit events.
- Add timeline-event based issue association, not only PR body/title parsing.
- Add duplicate notification tests for multiple workflow reruns.

## Configuration And Doctor

- Extend `doctor` to validate:
  - asset repo write access
  - media vision command availability
  - `ffmpeg` when video tests or video processing are enabled
  - `codex` or `claude` provider auth on the runner
  - GitHub Issue Field option drift
- Add `validate-config --live` for optional external checks.
- Add examples for a minimal project config and a full project config.

## Documentation

- Keep `README.md` project-neutral.
- Keep project-specific rollout notes outside this repo.
- Add a runner setup guide.
- Add a daemon setup guide for `watch-lark`.
- Add an operations guide for replay/backfill/rollback.
