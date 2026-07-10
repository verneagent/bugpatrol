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

## Outage Recovery

When the watcher was down or a webhook delivery was missed, two idempotent
reconcile passes bring GitHub and Lark back in sync. Both skip work that already
completed (managed-issue markers and `BUGPATROL_FIX_META` dedup), so re-running
them is safe.

Replay triage for intook issues that never produced a triage result:

```bash
python -m bugpatrol reconcile-triage projects/example.toml \
  --repo-path /path/to/project \
  --output-dir .bugpatrol/runs/reconcile \
  --execute
```

Replay fix notifications from GitHub state — recently merged PRs, closed
managed issues, and their linked commits — instead of a hand-authored JSON file:

```bash
python -m bugpatrol reconcile-fix-notifications projects/example.toml \
  --from-github \
  --write
```

Preview either without applying by dropping `--execute` / `--write`. Bound the
GitHub scan with `--pr-limit` and `--closed-issue-limit` on large repos. The
`examples/github-actions/bugpatrol-reconcile.yml` workflow runs both on a
schedule (and on manual dispatch) as a standing safety net.

## Rollback

Stop the watcher first. Then:

- remove only test assets created by the run
- close or edit only issues created by the run
- delete or move local state files if replaying from scratch
- restart exactly one watcher writer
