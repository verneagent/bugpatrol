# Operations

## Dry Run

Read Lark history without writing GitHub or Lark state:

```bash
python -m bugpatrol backfill-lark projects/example.toml --limit 50
```

## Backfill

Write recent Lark topics to GitHub:

```bash
python -m bugpatrol backfill-lark projects/example.toml \
  --limit 50 \
  --write \
  --asset-repo
```

## Replay Triage

Generate context without applying results:

```bash
python -m bugpatrol run-triage projects/example.toml \
  --issue 123 \
  --repo-path /path/to/project \
  --output-dir .bugpatrol/triage-123
```

Apply only after reviewing the output:

```bash
python -m bugpatrol run-triage projects/example.toml \
  --issue 123 \
  --repo-path /path/to/project \
  --output-dir .bugpatrol/triage-123 \
  --execute
```

## Validate A Triage Result

```bash
python -m bugpatrol apply-triage-result projects/example.toml \
  --issue 123 \
  --input .bugpatrol/triage-123/triage-output.json \
  --dry-run
```

## Rollback

Stop the watcher first. Then:

- remove only test assets created by the run
- close or edit only issues created by the run
- delete or move local state files if replaying from scratch
- restart exactly one watcher writer
