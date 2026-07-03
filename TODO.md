# bugpatrol TODO

Project-neutral product work that remains after the current intake, triage,
media, and fix-notification slices.

## Follow-Up After Triage

- When material evidence arrives after `Done` or `Skipped`, move status to
  `Needs review`.
- Improve the deterministic follow-up classifier with configurable project
  keyword rules while keeping the default classifier project-neutral.

## Watcher Robustness

- Add reconnect/backoff and heartbeat handling for WebSocket mode.

## Triage Automation

- Add active-run metadata so concurrent triage jobs for the same issue do not
  overwrite each other.

## Media Handling

- Add configurable image resizing before vision and upload.
- Add video duration limits.
- Add video frame extraction or clipping for large videos.
- Add retry policy for transient vision API failures.
- Add media redaction hooks for sensitive screenshots.
- Add attachment cleanup/reconcile command for test assets.

## Fix Notifications

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
