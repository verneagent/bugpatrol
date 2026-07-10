# Runner Setup

BugPatrol uses one-shot jobs for triage and fix notification.

## Triage Runner

Use a GitHub Actions self-hosted runner when triage needs local repo context or
pre-authenticated agent tooling.

Required on the runner:

- Python 3.11+
- `git`
- configured agent provider, such as `codex login`
- media vision credentials if `[media].description_command` needs them

Everything else comes from the workflow, not the runner disk:

- **GitHub auth**: minted in-workflow with `actions/create-github-app-token`
  (App id + private key in Actions secrets). No private key or `gh` login on the
  box. The token authorizes issue/comment/field writes and the on-demand
  private-branch fetch during branch resolution.
- **Agent + Lark secrets**: `DEEPSEEK_API_KEY` and the Lark app secret are
  injected into the step env from Actions secrets; the code reads both from
  `os.environ`. Keep only machine-specific non-secrets (proxy vars, runner name)
  in the runner `.env`.
- **App repo checkout**: a runner-owned, per-runner cache clone bootstrapped by
  `examples/github-actions/bugpatrol-cache-bootstrap.sh` — not a human dev
  checkout. The cache is namespaced by `$RUNNER_NAME`
  (`$HOME/.bugpatrol-cache/$RUNNER_NAME/<owner>/<repo>`) so two runners on one
  machine never share a clone; a self-hosted runner is single-concurrency, so
  every git op stays serial and race-free. The clone keeps full history (branch
  resolution needs it), origin stays a tokenless URL, and auth is a repo-local
  credential helper that echoes `GH_TOKEN` at call time — never `git config
  --global`, and no token persisted in `.git/config`.
- **PRD/docs**: the full-history clone already contains everything under the
  configured `[prd].cache_path`.

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
