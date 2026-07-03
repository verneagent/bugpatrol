"""Command-line entry point for bugpatrol."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from bugpatrol.agents import build_triage_agent_invocation
from bugpatrol.backfill import run_lark_backfill
from bugpatrol.config import load_project_config
from bugpatrol.doctor import run_doctor
from bugpatrol.event_watcher import iter_json_event_lines, run_lark_event_watcher
from bugpatrol.fields import TRIAGE_OUTPUT_SCHEMA, default_field_specs
from bugpatrol.fix_notify import FIX_EVENTS, apply_fix_notification, resolve_single_issue_from_pr
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkOpenApiMessengerClient
from bugpatrol.ownership import load_codeowners, resolve_owners
from bugpatrol.prd import load_prd_documents, search_prd_documents
from bugpatrol.resources import CommandResourceDescriber, GitHubAssetRepoStore
from bugpatrol.triage_context import build_triage_context, render_triage_context_markdown
from bugpatrol.triage_result import apply_triage_result, build_triage_dry_run_report, parse_triage_result
from bugpatrol.triage_runner import execute_triage_run, prepare_triage_run
from bugpatrol.watcher import run_polling_watcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bugpatrol")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-config", help="validate a project TOML config")
    validate.add_argument("path", type=Path)

    schema = sub.add_parser("schema", help="print the triage JSON schema")
    schema.add_argument("--pretty", action="store_true")

    agent = sub.add_parser("agent-command", help="print the configured triage agent command")
    agent.add_argument("project_config", type=Path)
    agent.add_argument("--issue", type=int, required=True)
    agent.add_argument("--prompt", type=Path, default=Path("prompts/triage.zh.md"))
    agent.add_argument("--schema", type=Path, default=Path("triage.schema.json"))
    agent.add_argument("--output", type=Path, default=Path("triage-output.json"))
    agent.add_argument("--context", type=Path)

    backfill = sub.add_parser("backfill-lark", help="backfill recent Lark messages into GitHub")
    backfill.add_argument("project_config", type=Path)
    backfill.add_argument("--limit", type=int, default=20)
    backfill.add_argument("--write", action="store_true", help="perform writes; default is dry-run")
    backfill.add_argument("--resource-dir", type=Path, help="download Lark resources before writing issues")
    backfill.add_argument("--asset-repo", action="store_true", help="upload Lark resources to configured assets repo")

    doctor = sub.add_parser("doctor", help="check project integration dependencies")
    doctor.add_argument("project_config", type=Path)
    doctor.add_argument("--with-lark", action="store_true")

    watch = sub.add_parser("watch-lark", help="poll Lark and mirror messages into GitHub")
    watch.add_argument("project_config", type=Path)
    watch.add_argument("--limit", type=int, default=20)
    watch.add_argument("--interval", type=float, default=30)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--dry-run", action="store_true", help="scan without GitHub writes")
    watch.add_argument("--resource-dir", type=Path, help="download Lark resources before writing issues")
    watch.add_argument("--asset-repo", action="store_true", help="upload Lark resources to configured assets repo")
    watch.add_argument("--event-log", type=Path, help="append structured watcher events to a JSONL file")
    watch.add_argument("--processed-ledger", type=Path, help="persist processed Lark message ids in this JSON file")
    watch.add_argument("--lease-file", type=Path, help="single-writer lease file for this watcher")
    watch.add_argument("--lease-ttl-seconds", type=float, default=120)
    watch.add_argument("--triage-queue", type=Path, help="persist debounced triage requests in this JSON file")
    watch.add_argument("--triage-quiet-seconds", type=float, default=60)
    watch.add_argument(
        "--triage-dispatch-command",
        nargs="+",
        help=(
            "command used for due triage requests; supports {issue_number}, "
            "{trigger_fingerprint}, and {reason}"
        ),
    )

    event_watch = sub.add_parser("watch-lark-events", help="read Lark event NDJSON from stdin into GitHub")
    event_watch.add_argument("project_config", type=Path)
    event_watch.add_argument("--dry-run", action="store_true", help="scan without GitHub writes")
    event_watch.add_argument("--resource-dir", type=Path, help="download Lark resources before writing issues")
    event_watch.add_argument("--asset-repo", action="store_true", help="upload Lark resources to configured assets repo")
    event_watch.add_argument("--event-log", type=Path, help="append structured watcher events to a JSONL file")
    event_watch.add_argument("--processed-ledger", type=Path, help="persist processed Lark message ids in this JSON file")
    event_watch.add_argument("--triage-queue", type=Path, help="persist debounced triage requests in this JSON file")
    event_watch.add_argument("--triage-quiet-seconds", type=float, default=60)
    event_watch.add_argument(
        "--triage-dispatch-command",
        nargs="+",
        help=(
            "command used for due triage requests; supports {issue_number}, "
            "{trigger_fingerprint}, and {reason}"
        ),
    )

    owner = sub.add_parser("resolve-owner", help="resolve owners for paths using CODEOWNERS")
    owner.add_argument("repo_path", type=Path)
    owner.add_argument("paths", nargs="+")

    prd = sub.add_parser("search-prd", help="search local PRD markdown docs")
    prd.add_argument("root", type=Path)
    prd.add_argument("query")
    prd.add_argument("--limit", type=int, default=5)
    prd.add_argument("--include-glob", action="append", default=None)

    context = sub.add_parser("issue-context", help="build triage context markdown for an issue")
    context.add_argument("project_config", type=Path)
    context.add_argument("--issue", type=int, required=True)
    context.add_argument("--repo-path", type=Path, required=True)
    context.add_argument("--output", type=Path)

    apply_result = sub.add_parser("apply-triage-result", help="validate and apply triage JSON")
    apply_result.add_argument("project_config", type=Path)
    apply_result.add_argument("--issue", type=int, required=True)
    apply_result.add_argument("--input", type=Path, required=True)
    apply_result.add_argument("--dry-run", action="store_true", help="validate and report changes without writing")

    run_triage = sub.add_parser("run-triage", help="prepare and optionally execute triage")
    run_triage.add_argument("project_config", type=Path)
    run_triage.add_argument("--issue", type=int, required=True)
    run_triage.add_argument("--repo-path", type=Path, required=True)
    run_triage.add_argument("--output-dir", type=Path, default=Path(".bugpatrol/triage-run"))
    run_triage.add_argument("--execute", action="store_true")

    notify_fix = sub.add_parser("notify-fix", help="notify Lark about explicit fix progress")
    notify_fix.add_argument("project_config", type=Path)
    notify_fix.add_argument("--issue", type=int)
    notify_fix.add_argument("--event", choices=FIX_EVENTS, required=True)
    notify_fix.add_argument("--pr", default="")
    notify_fix.add_argument("--commit", default="")
    notify_fix.add_argument("--write", action="store_true", help="send Lark notification and write metadata")

    args = parser.parse_args(argv)

    if args.command == "validate-config":
        config = load_project_config(args.path)
        config.validate_against(default_field_specs())
        print(f"ok: {config.project}")
        return 0

    if args.command == "schema":
        if args.pretty:
            print(json.dumps(TRIAGE_OUTPUT_SCHEMA, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(TRIAGE_OUTPUT_SCHEMA, ensure_ascii=False, separators=(",", ":")))
        return 0

    if args.command == "agent-command":
        config = load_project_config(args.project_config)
        invocation = build_triage_agent_invocation(
            config,
            issue_number=args.issue,
            prompt_path=args.prompt,
            schema_path=args.schema,
            output_path=args.output,
            context_path=args.context,
        )
        print(json.dumps({"provider": invocation.provider, "command": invocation.command}, ensure_ascii=False))
        return 0

    if args.command == "backfill-lark":
        config = load_project_config(args.project_config)
        app_secret = os.environ.get(config.lark.app_secret_env)
        if not app_secret:
            print(f"missing env: {config.lark.app_secret_env}", file=sys.stderr)
            return 2
        lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        github = GitHubCliIssuesClient(
            issue_fields=GitHubIssueFieldsClient(),
            project_config=config,
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        if args.resource_dir and args.asset_repo:
            print("--resource-dir and --asset-repo are mutually exclusive", file=sys.stderr)
            return 2
        resource_store = None
        if args.asset_repo:
            if not config.assets.github_repo or not config.assets.checkout_path:
                print("missing [assets] github_repo or checkout_path", file=sys.stderr)
                return 2
            resource_store = GitHubAssetRepoStore(
                repo=config.assets.github_repo,
                checkout_path=Path(config.assets.checkout_path),
                base_path=config.assets.base_path,
                branch=config.assets.branch,
                remote_url=config.assets.remote_url,
            )
        resource_describer = None
        if config.media.description_command:
            temp_dir = Path(config.media.description_temp_dir) if config.media.description_temp_dir else None
            resource_describer = CommandResourceDescriber(
                command=config.media.description_command,
                timeout_seconds=config.media.description_timeout_seconds,
                temp_dir=temp_dir,
            )
        result = run_lark_backfill(
            config=config,
            lark=lark,
            workflow=workflow,
            limit=args.limit,
            dry_run=not args.write,
            resource_dir=args.resource_dir,
            resource_store=resource_store,
            resource_describer=resource_describer,
            event_log_path=args.event_log,
            processed_ledger_path=args.processed_ledger,
            lease_file=args.lease_file,
            lease_ttl_seconds=args.lease_ttl_seconds,
            triage_queue_path=args.triage_queue,
            triage_quiet_seconds=args.triage_quiet_seconds,
            triage_dispatch_command=args.triage_dispatch_command,
        )
        print(
            json.dumps(
                {
                    "dry_run": not args.write,
                    "scanned": result.scanned,
                    "processed": result.processed,
                    "skipped": result.skipped,
                    "issues": [
                        {
                            "action": outcome.action,
                            "number": outcome.issue.number,
                            "url": outcome.issue.url,
                        }
                        for outcome in result.outcomes
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "doctor":
        config = load_project_config(args.project_config)
        lark = None
        if args.with_lark:
            app_secret = os.environ.get(config.lark.app_secret_env)
            if not app_secret:
                print(f"missing env: {config.lark.app_secret_env}", file=sys.stderr)
                return 2
            lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        checks = run_doctor(
            config=config,
            github=GitHubCliIssuesClient(),
            issue_fields=GitHubIssueFieldsClient(),
            lark=lark,
        )
        print(json.dumps([check.__dict__ for check in checks], ensure_ascii=False))
        return 0 if all(check.ok for check in checks) else 1

    if args.command == "watch-lark":
        config = load_project_config(args.project_config)
        app_secret = os.environ.get(config.lark.app_secret_env)
        if not app_secret:
            print(f"missing env: {config.lark.app_secret_env}", file=sys.stderr)
            return 2
        lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        github = GitHubCliIssuesClient(
            issue_fields=GitHubIssueFieldsClient(),
            project_config=config,
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        if args.resource_dir and args.asset_repo:
            print("--resource-dir and --asset-repo are mutually exclusive", file=sys.stderr)
            return 2
        resource_store = None
        if args.asset_repo:
            if not config.assets.github_repo or not config.assets.checkout_path:
                print("missing [assets] github_repo or checkout_path", file=sys.stderr)
                return 2
            resource_store = GitHubAssetRepoStore(
                repo=config.assets.github_repo,
                checkout_path=Path(config.assets.checkout_path),
                base_path=config.assets.base_path,
                branch=config.assets.branch,
                remote_url=config.assets.remote_url,
            )
        resource_describer = None
        if config.media.description_command:
            temp_dir = Path(config.media.description_temp_dir) if config.media.description_temp_dir else None
            resource_describer = CommandResourceDescriber(
                command=config.media.description_command,
                timeout_seconds=config.media.description_timeout_seconds,
                temp_dir=temp_dir,
            )
        result = run_polling_watcher(
            config=config,
            lark=lark,
            workflow=workflow,
            limit=args.limit,
            interval_seconds=args.interval,
            once=args.once,
            dry_run=args.dry_run,
            resource_dir=args.resource_dir,
            resource_store=resource_store,
            resource_describer=resource_describer,
        )
        print(json.dumps(result.__dict__, ensure_ascii=False))
        return 0

    if args.command == "watch-lark-events":
        config = load_project_config(args.project_config)
        app_secret = os.environ.get(config.lark.app_secret_env)
        if not app_secret:
            print(f"missing env: {config.lark.app_secret_env}", file=sys.stderr)
            return 2
        lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        github = GitHubCliIssuesClient(
            issue_fields=GitHubIssueFieldsClient(),
            project_config=config,
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        if args.resource_dir and args.asset_repo:
            print("--resource-dir and --asset-repo are mutually exclusive", file=sys.stderr)
            return 2
        resource_store = None
        if args.asset_repo:
            if not config.assets.github_repo or not config.assets.checkout_path:
                print("missing [assets] github_repo or checkout_path", file=sys.stderr)
                return 2
            resource_store = GitHubAssetRepoStore(
                repo=config.assets.github_repo,
                checkout_path=Path(config.assets.checkout_path),
                base_path=config.assets.base_path,
                branch=config.assets.branch,
                remote_url=config.assets.remote_url,
            )
        resource_describer = None
        if config.media.description_command:
            temp_dir = Path(config.media.description_temp_dir) if config.media.description_temp_dir else None
            resource_describer = CommandResourceDescriber(
                command=config.media.description_command,
                timeout_seconds=config.media.description_timeout_seconds,
                temp_dir=temp_dir,
            )
        result = run_lark_event_watcher(
            config=config,
            event_payloads=iter_json_event_lines(sys.stdin),
            lark=lark,
            workflow=workflow,
            dry_run=args.dry_run,
            resource_dir=args.resource_dir,
            resource_store=resource_store,
            resource_describer=resource_describer,
            event_log_path=args.event_log,
            processed_ledger_path=args.processed_ledger,
            triage_queue_path=args.triage_queue,
            triage_quiet_seconds=args.triage_quiet_seconds,
            triage_dispatch_command=args.triage_dispatch_command,
        )
        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    "scanned": result.scanned,
                    "processed": result.processed,
                    "skipped": result.skipped,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "resolve-owner":
        rules = load_codeowners(args.repo_path)
        print(
            json.dumps(
                {
                    path: list(resolve_owners(path, rules))
                    for path in args.paths
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "search-prd":
        docs = load_prd_documents(args.root, include_globs=tuple(args.include_glob or ("**/*.md",)))
        hits = search_prd_documents(args.query, docs, limit=args.limit)
        print(json.dumps([hit.__dict__ for hit in hits], ensure_ascii=False))
        return 0

    if args.command == "issue-context":
        config = load_project_config(args.project_config)
        github = GitHubCliIssuesClient()
        issue = github.get_issue(repo=config.github_repo, issue_number=args.issue)
        comments = github.list_issue_comments(repo=config.github_repo, issue_number=args.issue)
        prd_root = args.repo_path / config.prd.cache_path
        context = build_triage_context(
            issue=issue,
            comments=comments,
            prd_root=prd_root,
            prd_include_globs=config.prd.include_globs,
        )
        markdown = render_triage_context_markdown(context)
        if args.output:
            args.output.write_text(markdown)
        else:
            print(markdown)
        return 0

    if args.command == "apply-triage-result":
        config = load_project_config(args.project_config)
        data = json.loads(args.input.read_text())
        result = parse_triage_result(data)
        if args.dry_run:
            report = build_triage_dry_run_report(
                repo=config.github_repo,
                issue_number=args.issue,
                config=config,
                result=result,
                issue_fields=GitHubIssueFieldsClient(),
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "issue": args.issue,
                        "issue_type": report.issue_type,
                        "assignee": report.assignee,
                        "field_changes": [change.__dict__ for change in report.field_changes],
                        "comment_markdown": report.comment_markdown,
                        "result_fingerprint": report.result_fingerprint,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        summary = apply_triage_result(
            repo=config.github_repo,
            issue_number=args.issue,
            config=config,
            result=result,
            github=GitHubCliIssuesClient(),
            issue_fields=GitHubIssueFieldsClient(),
        )
        print(json.dumps({"ok": True, "issue": args.issue, "summary": summary.__dict__}, ensure_ascii=False))
        return 0

    if args.command == "run-triage":
        config = load_project_config(args.project_config)
        github = GitHubCliIssuesClient()
        issue_fields = GitHubIssueFieldsClient()
        plan = prepare_triage_run(
            config=config,
            issue_number=args.issue,
            repo_path=args.repo_path,
            output_dir=args.output_dir,
            github=github,
        )
        if args.execute:
            execute_triage_run(
                config=config,
                issue_number=args.issue,
                plan=plan,
                github=github,
                issue_fields=issue_fields,
            )
        print(
            json.dumps(
                {
                    "execute": args.execute,
                    "context": str(plan.context_path),
                    "schema": str(plan.schema_path),
                    "output": str(plan.output_path),
                    "provider": plan.invocation.provider,
                    "command": plan.invocation.command,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "notify-fix":
        config = load_project_config(args.project_config)
        github = GitHubCliIssuesClient()
        lark = None
        if args.write:
            app_secret = os.environ.get(config.lark.app_secret_env)
            if not app_secret:
                print(f"missing env: {config.lark.app_secret_env}", file=sys.stderr)
                return 2
            lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        issue_number = args.issue
        if issue_number is None:
            if args.event not in ("pr_opened", "pr_merged") or not args.pr:
                print("--issue is required unless --event is pr_opened/pr_merged with --pr", file=sys.stderr)
                return 2
            try:
                issue_number = resolve_single_issue_from_pr(
                    github.get_pull_request(repo=config.github_repo, pr=args.pr)
                )
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 2
        summary = apply_fix_notification(
            repo=config.github_repo,
            issue_number=issue_number,
            event=args.event,
            pr=args.pr,
            commit=args.commit,
            dry_run=not args.write,
            github=github,
            lark=lark,
        )
        print(json.dumps(summary.__dict__, ensure_ascii=False))
        return 0

    parser.print_help(file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
