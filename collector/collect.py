#!/usr/bin/env python3
"""Collect GitHub repository statistics into PostgreSQL for Grafana.

GitHub keeps traffic data (views, clones, referrers, paths) for 14 days only,
and release download counts are a running total. This script runs hourly and
stores snapshots so the history survives.

Tokens: one file per repo owner in TOKEN_DIR (fine-grained PAT, Administration:
read for traffic), file "default" as fallback. Repos without push access (other
people's) get only the public data; their traffic calls return 403 and are skipped.
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
    pass


def token_for(repo):
    d = os.environ.get("TOKEN_DIR", "/var/run/ghstats/tokens")
    for name in (repo.split("/")[0], "default"):
        try:
            with open(os.path.join(d, name)) as f:
                return f.read().strip()
        except FileNotFoundError:
            pass
    raise RuntimeError(f"{repo}: no token in {d} for owner or default")


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
            raise Forbidden(f"GET {path}: 403") from e
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
    today = now.date()
    token = token_for(repo)

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

    try:
        traffic(cur, repo, token, today)
    except Forbidden:
        print(f"{repo}: no push access, traffic skipped", file=sys.stderr)

    print(f"{repo}: stars={r['stargazers_count']} assets={n}", file=sys.stderr)


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
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(SCHEMA)
        for repo in repos:
            with conn.transaction(), conn.cursor() as cur:
                collect(cur, repo, now)


if __name__ == "__main__":
    main()
