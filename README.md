# ghstats

Collects GitHub repository statistics into PostgreSQL, so they can be shown in
Grafana over a longer period than GitHub keeps them.

GitHub shows traffic (views, clones, referrers, popular pages) for the last
14 days only, and release download counts are a running total without history.
`ghstats` runs periodically (for example as a Kubernetes CronJob every hour) and
stores snapshots:

| Table | Content |
|---|---|
| `repo_snapshot` | stars, forks, watchers, open issues per run |
| `release_downloads` | download count of every release asset per run |
| `traffic_daily` | views and clones per day (count and uniques) |
| `referrers` | top referrers, 14-day totals, one set per day |
| `popular_paths` | popular pages, 14-day totals, one set per day |

The schema is created on the first run.

## Image

```
ghcr.io/movray/ghstats-collector:<version>
```

linux/amd64 and linux/arm64, runs as UID 65534 and works with a read-only root
filesystem.

## Configuration

| Variable | Meaning |
|---|---|
| `REPOS` | comma-separated list, e.g. `owner/repo,owner/other` |
| `DATABASE_URL` | PostgreSQL connection URI |
| `TOKEN_DIR` | directory with one token file per repository owner, file `default` as fallback (default `/var/run/ghstats/tokens`) |

Use a fine-grained personal access token per owner with access to the
repositories and **Administration: Read-only** (needed for traffic). For
repositories you have no push access to, the traffic calls return 403 and are
skipped; stars, forks and release downloads are still collected.

Each run makes about six API requests per repository, far below the rate limit
of 5,000 requests per hour.

## Example: Kubernetes CronJob

```yaml
containers:
  - name: collect
    image: ghcr.io/movray/ghstats-collector:0.1.0
    env:
      - name: REPOS
        value: owner/repo
      - name: DATABASE_URL
        valueFrom:
          secretKeyRef: {name: ghstats-db-app, key: uri}
    volumeMounts:
      - {name: tokens, mountPath: /var/run/ghstats/tokens, readOnly: true}
volumes:
  - name: tokens
    secret:
      secretName: ghstats-github   # keys: owner names
```

## License

Apache-2.0
