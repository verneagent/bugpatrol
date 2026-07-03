# bugpatrol TODO

Project-neutral product work that remains after the current intake, triage,
media, and fix-notification slices.

## Watcher Robustness

- Add reconnect/backoff and heartbeat handling for WebSocket mode.

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
  - `codex` or `claude` provider auth on the runner

## Documentation

- Keep `README.md` project-neutral.
- Keep project-specific rollout notes outside this repo.
