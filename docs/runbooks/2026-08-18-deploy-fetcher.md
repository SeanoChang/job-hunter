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
   uv run job-hunter doctor      # names the exact fix for whatever is missing
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
uv run job-hunter rebuild --yes
```

It holds the same advisory lock the fetcher takes, so it refuses to run while a
fetch is in flight (and vice versa) instead of interleaving writes. It builds
`jobhunter_new`, renames the live `jobhunter` to `jobhunter_previous`, and
renames the new schema into place; the swap is one transaction, so readers see
either the old store or the new one. `jobhunter_previous` is left behind as the
rollback copy and is dropped at the start of the next rebuild — check the new
store with `job-hunter q events --since 24h` before running another one.
`rebuild` refuses to run unattended without `--yes`.

> [!WARNING] Rebuild time grows with the archive
>
> Ingest issues ~6 single-row statements per record with no batching; a measured
> replay runs ~2.7 ms/record on loopback Postgres — roughly 7 hours for a year
> of personal-scale archive, and several times that over the network to Neon.
> Before the archive passes ~60 days, time a real `rebuild` and record the
> number here; if it is already painful, the fix is batching the per-record
> inserts (COPY / executemany), which is designed but not built. Known
> liability, accepted 2026-08-19.

## Extraction in CI (added 2026-08-27, folded into `sync` 2026-09-01)

The `fetch` workflow is now one `sync` step: it drains pending manifests,
fetches every board, and runs L2 extraction within budget, in that order — the
same choreography the three separate steps had, under the same two advisory
locks (ingestion's `jobh`, extraction's `job2`, never held together). L2 stays a
step rather than a second cron, because a separate schedule is another thing
that can die silently (durability doc §1).

Enable it by adding one secret:

- `JOB_HUNTER_L2_API_KEY` — an OpenRouter key. Buy the one-time $10 credit to
  lift the free tier from 50 to 1,000 requests/day; the retry ladder can spend
  up to 3 calls per document, so 50/day is only ~16 documents.

Until that secret exists the step passes `--no-extract` and only collects, so
the job stays green. Optional repository variables override the defaults without
editing the workflow: `JOB_HUNTER_L2_MODEL_CANDIDATES` (default
`z-ai/glm-5.2:free`), `JOB_HUNTER_L2_MODELS` (default `z-ai/*`),
`JOB_HUNTER_L2_BASE_URL`, `JOB_HUNTER_L2_MAX_DOCS` (default 50),
`JOB_HUNTER_L2_MAX_USD` (default 0 = free work only).

Run a canary from the Actions UI (or `gh workflow run fetch.yml -f
extract_max_docs=1`): the `extract_max_docs` input is passed as
`sync --extract-max-docs` for that run, and `0` leaves the extraction queue
untouched.

The step carries no `continue-on-error`, because `sync` itself makes the
distinction: collection is irreplaceable — history cannot be backfilled — while
extraction is recomputable from the archive at any time, so a bad engine day is
recorded as `data.extract.error` and the run still exits 0. It exits 6 only for
what an operator must act on: ingest gaps, a failed collection, or an extraction
engine that is stalled (breaker tripped, or every call throttled). Extraction
problems stay visible in the step log, in the `summary.json` artifact, and in
the `status` block that follows it.

## A read-only role for agents (added 2026-09-01)

`q` and `pulse` read; only `sync`/`fetch`/`ingest`/`rebuild`/`extract` write.
Agent machines get a role that cannot write, so a confused loop cannot damage
the corpus (`doctor` reports which kind of DSN it is holding):

```sql
CREATE ROLE jobhunter_ro LOGIN PASSWORD '…';
GRANT USAGE ON SCHEMA jobhunter TO jobhunter_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA jobhunter TO jobhunter_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA jobhunter GRANT SELECT ON TABLES TO jobhunter_ro;
```

`rebuild` swaps a whole schema into place, so re-run the two `GRANT` statements
after one (the `ALTER DEFAULT PRIVILEGES` line only covers tables created inside
the existing schema).

On the agent machine, put the read-only DSN and archive credentials in
`~/.config/job-hunter/env` (process env still wins, `./.env` sits between them)
so no shell profile has to be edited:

```bash
mkdir -p ~/.config/job-hunter && cat > ~/.config/job-hunter/env <<'EOF'
JOB_HUNTER_DATABASE_URL=postgresql://jobhunter_ro:…@…/jobhunter?sslmode=require
JOB_HUNTER_ARCHIVE_URL=s3://job-hunter/corpus
AWS_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=…
AWS_SECRET_ACCESS_KEY=…
AWS_DEFAULT_REGION=auto
EOF
chmod 600 ~/.config/job-hunter/env
job-hunter doctor        # role check should say read-only
job-hunter skill > ~/.claude/skills/job-hunter-cli/SKILL.md
```

Cursors are client state: `pulse --cursor <name>` keeps its watermark in
`$XDG_STATE_HOME/job-hunter/cursors.json` (override `JOB_HUNTER_STATE_DIR`), so
each machine has its own reading position and the shared store stays untouched.
