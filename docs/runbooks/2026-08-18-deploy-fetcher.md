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
3. **First run.** Actions → `fetch` → Run workflow. Check the job log: the
   `status` step should list every board with `ok`. Then verify locally:

   ```bash
   export JOB_HUNTER_ARCHIVE_URL=s3://job-hunter/corpus AWS_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com
   export AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… AWS_DEFAULT_REGION=auto
   uv run job-hunter status
   uv run job-hunter archive ls --board greenhouse:anthropic
   ```

4. **Daily.** The cron fires at 06:00 UTC. A red run means every board failed or
   the archive was unreachable; single-board failures show in `status`.
5. **Adding a company.** Edit `companies.toml`, run
   `uv run job-hunter registry check`, commit and push. The next run picks it up
   and archives a new registry revision.
6. **If the cron stops.** GitHub disables schedules after 60 idle days; the
   keepalive step prevents that, but if it happens: Actions → `fetch` → Enable
   workflow.
