---
title: Deploy the fetcher — R2 bucket, GitHub Actions cron
date: 2026-08-18
type: runbook
status: current
---

# Deploy the fetcher

1. **R2.** Cloudflare dashboard → R2 → create bucket `job-hunter` (any region).
   R2 → Manage API tokens → create a token with Object Read & Write scoped to
   that bucket. Note the Access Key ID, Secret Access Key, and the S3 endpoint
   `https://<account-id>.r2.cloudflarestorage.com`.
2. **GitHub → Settings → Secrets and variables → Actions.** Secrets:
   `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. Variables: `R2_ENDPOINT_URL` (the
   endpoint above), `JOB_HUNTER_ARCHIVE_URL` = `s3://job-hunter/corpus`.
3. **Neon.** Create a Neon project on Postgres 17 with a single database
   `jobhunter`. Copy the _direct_ connection string — the host **without** the
   `-pooler` suffix — and keep the `?sslmode=require` on it. Add it as the
   GitHub secret `JOB_HUNTER_DATABASE_URL`. It must not be the pooled string:
   the pooled endpoint is PgBouncer in transaction mode, and the store holds a
   session-level advisory lock and a session `search_path` across several
   transactions, neither of which transaction pooling preserves — the first
   `fetch` would fail on a missing relation and the single-writer lock would be
   meaningless. (Supporting a pooled DSN later is a follow-up: put
   `options=-c search_path=jobhunter,public` in the DSN and replace the session
   advisory lock with a transaction-scoped guard.) Create the schema once from
   your machine:

   ```bash
   export JOB_HUNTER_DATABASE_URL='postgresql://…?sslmode=require'
   uv run job-hunter db init
   ```

   The first `fetch` would create it too, so this is only to see the DDL apply
   before a cron run does it. Free tier: 0.5 GB storage and 100 CU-hours a month
   — the daily fetch of a few hundred postings is far under both.
   `job-hunter status` prints store health next to the per-board fetch health,
   so a DB problem shows up in the same place as a board problem.

4. **First run.** Actions → `fetch` → Run workflow. Check the job log: the
   `status` step should list every board with `ok`. Then verify locally:

   ```bash
   export JOB_HUNTER_ARCHIVE_URL=s3://job-hunter/corpus AWS_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com
   export AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… AWS_DEFAULT_REGION=auto
   uv run job-hunter status
   uv run job-hunter archive ls --board greenhouse:anthropic
   ```

5. **Daily.** The cron fires at 06:00 UTC. A red run means every board failed or
   the archive was unreachable; single-board failures show in `status`.
6. **Adding a company.** Edit `companies.toml`, run
   `uv run job-hunter registry check`, commit and push. The next run picks it up
   and archives a new registry revision.
7. **If the cron stops.** GitHub disables schedules after 60 idle days; the
   keepalive step prevents that, but if it happens: Actions → `fetch` → Enable
   workflow.

## Rebuild

`rebuild` replays the whole archive into a fresh schema and swaps it live, which
is how a corrected normalizer, a schema change or a bad ingest is repaired — the
archive is the truth, the store is derived.

```bash
uv run job-hunter rebuild
```

It holds the same advisory lock the fetcher takes, so it refuses to run while a
fetch is in flight (and vice versa) instead of interleaving writes. It builds
`jobhunter_new`, renames the live `jobhunter` to `jobhunter_previous`, and
renames the new schema into place; the swap is one transaction, so readers see
either the old store or the new one. `jobhunter_previous` is left behind as the
rollback copy and is dropped at the start of the next rebuild — check the new
store with `job-hunter report --since 24h` before running another one.
