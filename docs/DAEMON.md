# Daemon Setup

BugPatrol uses a long-running watcher daemon for Lark intake.

## Polling Watcher

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

Run exactly one active watcher writer per Lark group. Use `--lease-file` to
prevent accidental duplicate writers on one host.

## Event Stream Watcher

Use event stream mode behind a Lark WebSocket/event client:

```bash
lark-cli event listen --format ndjson \
  | python -m bugpatrol watch-lark-events projects/example.toml \
      --asset-repo \
      --event-log .bugpatrol/watch-events.jsonl \
      --processed-ledger .bugpatrol/processed-messages.json \
      --triage-queue .bugpatrol/triage-queue.json
```

Heartbeat payloads such as `{"type":"heartbeat"}` or `{"header":{"event_type":"ping"}}`
are ignored by `watch-lark-events`. In-process WebSocket adapters should wrap
their subscription with `iter_reconnecting_event_payloads` so transient socket
closures reconnect with exponential backoff before handing NDJSON lines to the
watcher.

Keep polling mode available as a fallback.

## State Files

- `watch-events.jsonl`: structured operational log.
- `watch-lark.lock`: local single-writer lease.
- `processed-messages.json`: durable processed Lark message ledger.
- `triage-queue.json`: debounced triage request queue.
