#!/usr/bin/env python3
"""Collect GitHub repository statistics into PostgreSQL for Grafana.

GitHub keeps traffic data (views, clones, referrers, paths) for 14 days only,
and release download counts are a running total. This script runs hourly and
stores snapshots so the history survives.

Tokens: one file per repo owner in TOKEN_DIR (fine-grained PAT, Administration:
read for traffic), file "default" as fallback. Repos of an owner with their own
token file are expected to deliver traffic: a 403 there fails the run and names
the missing permission. Repos only covered by "default" (other people's) get the
public data; their traffic is skipped.
Env: REPOS (comma-separated owner/name), DATABASE_URL (CNPG app secret "uri"),
     TOKEN_DIR (default /var/run/ghstats/tokens).
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import psycopg

API = "https://api.github.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_snapshot (
    ts timestamptz NOT NULL, repo text NOT NULL,
    stars int, forks int, watchers int, open_issues int,
    PRIMARY KEY (repo, ts));
CREATE TABLE IF NOT EXISTS release_downloads (
    ts timestamptz NOT NULL, repo text NOT NULL, tag text NOT NULL,
    asset text NOT NULL, prerelease bool, downloads int,
    PRIMARY KEY (repo, ts, tag, asset));
CREATE TABLE IF NOT EXISTS traffic_daily (
    day date NOT NULL, repo text NOT NULL, kind text NOT NULL,
    count int, uniques int,
    PRIMARY KEY (repo, kind, day));
CREATE TABLE IF NOT EXISTS referrers (
    day date NOT NULL, repo text NOT NULL, referrer text NOT NULL,
    count int, uniques int,
    PRIMARY KEY (repo, day, referrer));
CREATE TABLE IF NOT EXISTS popular_paths (
    day date NOT NULL, repo text NOT NULL, path text NOT NULL,
    title text, count int, uniques int,
    PRIMARY KEY (repo, day, path));
"""


class Forbidden(RuntimeError):
    def __init__(self, path, needs):
        self.needs = needs
        super().__init__(f"GET {path}: 403" + (f", token needs {needs}" if needs else ""))


def token_for(repo):
    """Return (token, own): own is True if the owner has its own token file."""
    d = os.environ.get("TOKEN_DIR", "/var/run/ghstats/tokens")
    for name in (repo.split("/")[0], "default"):
        try:
            with open(os.path.join(d, name)) as f:
                return f.read().strip(), name != "default"
        except FileNotFoundError:
            pass
    raise RuntimeError(f"no token in {d} for owner or default")


def gh(path, token):
    req = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ghstats-collector",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise Forbidden(path, e.headers.get("X-Accepted-GitHub-Permissions")) from e
        raise RuntimeError(f"GET {path}: {e.code} {e.read()[:200]!r}") from e


def releases(repo, token):
    page = 1
    while True:
        batch = gh(f"/repos/{repo}/releases?per_page=100&page={page}", token)
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def collect(cur, repo, now):
    """Store one snapshot of repo; return an error message or None."""
    today = now.date()
    token, own = token_for(repo)

    r = gh(f"/repos/{repo}", token)
    cur.execute(
        "INSERT INTO repo_snapshot VALUES (%s, %s, %s, %s, %s, %s)",
        (now, repo, r["stargazers_count"], r["forks_count"],
         r["subscribers_count"], r["open_issues_count"]))

    n = 0
    for rel in releases(repo, token):
        for a in rel["assets"]:
            cur.execute(
                "INSERT INTO release_downloads VALUES (%s, %s, %s, %s, %s, %s)",
                (now, repo, rel["tag_name"], a["name"], rel["prerelease"],
                 a["download_count"]))
            n += 1

    print(f"{repo}: stars={r['stargazers_count']} assets={n}", file=sys.stderr)
    try:
        with cur.connection.transaction():
            traffic(cur, repo, token, today)
    except Forbidden as e:
        if own:
            return f"traffic skipped: {e}"
        print(f"{repo}: no push access (default token), traffic skipped", file=sys.stderr)
    return None


def traffic(cur, repo, token, today):
    # Daily views/clones: the last 14 days, the current day is partial.
    for kind in ("views", "clones"):
        for d in gh(f"/repos/{repo}/traffic/{kind}?per=day", token)[kind]:
            cur.execute(
                """INSERT INTO traffic_daily VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (repo, kind, day)
                   DO UPDATE SET count = EXCLUDED.count, uniques = EXCLUDED.uniques""",
                (d["timestamp"][:10], repo, kind, d["count"], d["uniques"]))

    # Referrers and paths are 14-day totals; keep the last value per day.
    cur.execute("DELETE FROM referrers WHERE repo = %s AND day = %s", (repo, today))
    for ref in gh(f"/repos/{repo}/traffic/popular/referrers", token):
        cur.execute("INSERT INTO referrers VALUES (%s, %s, %s, %s, %s)",
                    (today, repo, ref["referrer"], ref["count"], ref["uniques"]))
    cur.execute("DELETE FROM popular_paths WHERE repo = %s AND day = %s", (repo, today))
    for p in gh(f"/repos/{repo}/traffic/popular/paths", token):
        cur.execute("INSERT INTO popular_paths VALUES (%s, %s, %s, %s, %s, %s)",
                    (today, repo, p["path"], p["title"], p["count"], p["uniques"]))


def main():
    repos = [r.strip() for r in os.environ["REPOS"].split(",") if r.strip()]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    failed = 0
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(SCHEMA)
        conn.commit()
        for repo in repos:
            # A missing traffic permission keeps the other data of the repo;
            # any other error rolls the repo back. Both fail the run.
            try:
                with conn.transaction(), conn.cursor() as cur:
                    err = collect(cur, repo, now)
            except Exception as e:
                err = str(e)
            if err:
                print(f"{repo}: ERROR {err}", file=sys.stderr)
                failed += 1
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
