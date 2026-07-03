# Runner Setup

BugPatrol uses one-shot jobs for triage and fix notification.

## Triage Runner

Use a GitHub Actions self-hosted runner when triage needs local repo context or
pre-authenticated agent tooling.

Required on the runner:

- Python 3.11+
- `gh` authenticated with issue, comment, and Issue Fields write access
- local project config path, exported through `BUGPATROL_PROJECT_CONFIG`
- the app repo checkout
- local PRD/docs checkout under the configured `[prd].cache_path`
- configured agent provider, such as `codex login`
- media vision credentials if `[media].description_command` needs them

Recommended workflow template:

```text
examples/github-actions/bugpatrol-triage.yml
```

## Fix Notification Runner

Fix notification can usually run on GitHub-hosted runners. It only needs GitHub
and Lark credentials plus the project config.

Recommended workflow template:

```text
examples/github-actions/bugpatrol-notify-fix.yml
```

## Concurrency

Use one concurrency group per issue for triage. Do not cancel in-progress triage;
BugPatrol defers new material follow-up while the issue is `Running`.
