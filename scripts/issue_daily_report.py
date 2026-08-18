#!/usr/bin/env python3
"""Daily per-reporter issue count for a GitHub repo.

Answers "who sent how many issues today" for a repo fed by BugPatrol (or any
repo whose issues are created by a bot). Two reporter sources are handled:

- **BugPatrol intake**: the issue body carries a hidden
  ``<!-- BUGPATROL_INTAKE_META:{"reporter_open_id": ...} -->`` comment; the
  reporter is that open_id (the GitHub author is the bot).
- **Native**: the issue has no intake meta; the reporter is ``issue.user.login``.

Reporter display names come from, in priority order:
``[lark.sender_names]`` -> reverse ``[lark.user_open_ids]`` (open_id -> login)
-> the raw id.

Self-contained: stdlib only, shells out to ``gh`` for API + auth.

Usage::

    python scripts/issue_daily_report.py                  # today, TheCloverLab/fived
    python scripts/issue_daily_report.py --project projects/fived.toml
    python scripts/issue_daily_report.py --since 2026-08-17
    python scripts/issue_daily_report.py --format json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import zoneinfo

# Mirror of bugpatrol/intake.py:INTAKE_META_MARKER (kept duplicated so this
# script stays standalone and does not import the package).
INTAKE_META_MARKER = "BUGPATROL_INTAKE_META"
# Some clients store the comment with HTML-escaped brackets ("&lt;!--" / "--&gt;");
# BugPatrol writes literal "<!--". Tolerate both.
_META_RE = re.compile(
    r"(?:<!--|&lt;!--)\s*" + INTAKE_META_MARKER + r":(\{.*?\})\s*(?:--&gt;|-->)",
    re.DOTALL,
)

DEFAULT_REPO = "TheCloverLab/fived"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_PAGE = 100


def parse_intake_meta(body: str) -> dict | None:
    """Return the intake metadata dict embedded in an issue body, or None."""
    if not body:
        return None
    m = _META_RE.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def reporter_of(issue: dict) -> tuple[str | None, str]:
    """Return (reporter_id, source) for an issue.

    source is "meta" (BugPatrol reporter_open_id), "github" (issue author
    login), or "none".
    """
    meta = parse_intake_meta(issue.get("body") or "")
    if meta:
        open_id = meta.get("reporter_open_id")
        if open_id:
            return str(open_id), "meta"
    user = issue.get("user")
    if isinstance(user, str) and user:
        return user, "github"  # jq flattens user to a login string
    if isinstance(user, dict) and user.get("login"):
        return str(user["login"]), "github"
    return None, "none"


def load_project_config(path: str) -> dict:
    """Read github_repo, sender_names and user_open_ids from a bugpatrol
    project toml (stdlib tomllib)."""
    import tomllib

    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    out = {
        "repo": cfg.get("github_repo") or DEFAULT_REPO,
        "sender_names": dict(cfg.get("lark", {}).get("sender_names") or {}),
        "user_open_ids": dict(cfg.get("lark", {}).get("user_open_ids") or {}),
    }
    return out


def display_name(reporter_id: str, source: str, cfg: dict) -> str:
    """Resolve a reporter id to a human-readable name."""
    if source == "github":
        return reporter_id  # a login is already a name
    name = cfg.get("sender_names", {}).get(reporter_id)
    if name:
        return name
    # reverse [lark.user_open_ids]: open_id -> GitHub login
    reverse = {oid: login for login, oid in cfg.get("user_open_ids", {}).items()}
    return reverse.get(reporter_id, reporter_id)


def fetch_issues(repo: str, since_utc: str, gh_cmd: str) -> list[dict]:
    """List issues updated since since_utc via gh api (paginated).

    ``gh_cmd`` may be a wrapper script (e.g. gh-as-bot.sh) that injects its own
    auth. Read-only; raises on gh failure rather than swallowing it.
    """
    url = (
        f"repos/{repo}/issues"
        f"?state=all&since={since_utc}&per_page={DEFAULT_PAGE}"
    )
    # The projection below is a contract shared with aggregate()'s filters.
    # If a filter reads a key the projection doesn't emit, the filter silently
    # never fires (e.g. PRs sneaking in as "issues") -- so validate the shape
    # here and fail loud instead.
    required = {"number", "created_at", "user", "body", "is_pr"}
    cmd = [gh_cmd, "api", url, "--paginate", "--jq",
           ".[] | {number, created_at, user: .user.login, body, is_pr: (.pull_request != null)}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed ({proc.returncode}): {proc.stderr.strip()}")
    issues = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            issue = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"unparseable gh output line: {line!r}: {e}") from e
        if not isinstance(issue, dict):
            raise RuntimeError(f"gh output line is not an object: {line!r}")
        missing = required - issue.keys()
        if missing:
            raise RuntimeError(
                f"gh projection missing {sorted(missing)}; filters would be silently dead: {line[:200]!r}"
            )
        issues.append(issue)
    return issues


def aggregate(repo: str, since_dt: dt.datetime, gh_cmd: str, cfg: dict) -> dict:
    """Count issues created on/after since_dt (local tz), grouped by reporter."""
    # Bound the API fetch by updated_at (the `since` param) and filter locally
    # on created_at so only issues *created* in the window count.
    since_utc = since_dt.astimezone(dt.timezone.utc).isoformat()
    next_dt = since_dt + dt.timedelta(days=1)
    next_utc = next_dt.astimezone(dt.timezone.utc)
    issues = fetch_issues(repo, since_utc, gh_cmd)
    seen: dict[str, tuple[str, list[int]]] = {}
    for issue in issues:
        if issue.get("is_pr") or not issue.get("number"):
            continue  # PRs are not issues
        created = issue.get("created_at")
        if not created:
            continue
        if created.endswith("Z"):
            created = created[:-1] + "+00:00"  # naive "2026-08-18T02:00:00Z"
        created_dt = dt.datetime.fromisoformat(created)
        if created_dt < since_dt.astimezone(dt.timezone.utc) or created_dt >= next_utc:
            continue
        reporter_id, source = reporter_of(issue)
        if not reporter_id:
            continue
        if reporter_id not in seen:
            seen[reporter_id] = (source, [])
        seen[reporter_id][1].append(int(issue["number"]))
    counts = [
        {
            "reporter": display_name(rid, src, cfg),
            "open_id": rid if src == "meta" else None,
            "count": len(nums),
            "issues": nums,
        }
        for rid, (src, nums) in sorted(seen.items(), key=lambda kv: (-len(kv[1][1]), kv[0]))
    ]
    return {"repo": repo, "since": since_dt, "counts": counts}


def render_markdown(result: dict) -> str:
    since = result["since"].strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"### {result['repo']} · 今日上报（{since}）",
        "",
        "| 上报人 | Issue 数 | Issues |",
        "|---|---|---|",
    ]
    for c in result["counts"]:
        links = ", ".join(f"[#{n}](https://github.com/{result['repo']}/issues/{n})" for n in c["issues"])
        lines.append(f"| {c['reporter']} | {c['count']} | {links} |")
    total = sum(c["count"] for c in result["counts"])
    lines.extend(["", f"**合计 {total} 个 issue**"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", help="bugpatrol project toml (reads repo + name maps)")
    p.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo")
    p.add_argument("--since", help="start date YYYY-MM-DD (default: today in %(default_tz)s)")
    p.add_argument("--tz", default=DEFAULT_TZ, dest="default_tz", help="day-window timezone")
    p.add_argument("--gh", default="gh", dest="gh_cmd", help="gh binary or auth wrapper")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = p.parse_args(argv)

    cfg = {
        "repo": args.repo,
        "sender_names": {},
        "user_open_ids": {},
    }
    if args.project:
        cfg.update(load_project_config(args.project))
    tz = zoneinfo.ZoneInfo(args.default_tz)
    if args.since:
        day = dt.datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=tz)
    else:
        day = dt.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    result = aggregate(cfg["repo"], day, args.gh_cmd, cfg)
    if args.format == "json":
        out = {
            "repo": result["repo"],
            "since": result["since"].isoformat(),
            "counts": result["counts"],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
