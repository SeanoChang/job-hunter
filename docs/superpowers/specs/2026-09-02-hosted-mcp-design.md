# Hosted MCP server — design

Date: 2026-09-02. Status: approved design, pre-implementation.
Realizes the M4 MCP wrapper (2026-08-26 ruling: "after the verbs have been
exercised" — they now exist, shipped in the 2026-09-01 CLI rework and green in
CI). Motivation: Claude cloud routines run in ephemeral VMs with proxy-restricted
egress — client-side cursor files cannot persist between runs and raw Postgres
TCP from the sandbox is undocumented; an HTTPS MCP endpoint is the supported
path. Approved 2026-09-02: architecture B (hosted MCP reusing the package),
server-side cursors (deliberate bend of the "personal state stays on the
client" ruling — the server is owner infrastructure and the state is a
timestamp watermark), Slack DM delivery, deploy on **Cloud Run** (free tier
covers the traffic; Cloudflare Containers rejected at $5/mo baseline).

## 1. Shape

One new module serves the same read surface the CLI serves, over streamable
HTTP MCP, from the same package — zero duplicated SQL:

```
cli_q.py ──┐
           ├── views.py ── store/queries.py ── Postgres (Neon, read role)
mcp.py  ───┘         └──── pulse.py
```

- **`src/jobhunter/views.py`** (refactor): the payload-assembly code currently
  inline in `cli_q.py` command bodies moves here as pure functions
  `(conn, filters…) -> (data, truncated, next_cursor)`. CLI and MCP call the
  same functions; a parity test asserts identical output.
- **`src/jobhunter/mcp.py`** (new): FastMCP app (official `mcp` SDK, new
  runtime dependency) exposing eight tools mirroring the CLI verbs:
  `pulse`, `postings`, `posting`, `events`, `document`, `profile`, `claims`,
  `boards`. Same 50-default/500-hard limits, same truncation marking, same
  shapes as the CLI's `data` payloads. Read-only except `mcp_cursors`.
  Console script `job-hunter-mcp` runs it on `$PORT` (Cloud Run contract).

## 2. Auth

Static bearer token. Middleware rejects any request whose `Authorization`
header does not constant-time-match `Bearer $JOB_HUNTER_MCP_TOKEN` with 401;
`/healthz` is the only unauthenticated route (returns build version, no data).
The committed `.mcp.json` references the token as `${JOB_HUNTER_MCP_TOKEN}` —
the value exists only in the Cloud Run service config, Sean's cloud
environment variables, and his local shell. Never in git (repo is public).

## 3. Cursors — schema v4

```sql
CREATE TABLE IF NOT EXISTS mcp_cursors (
  name         TEXT PRIMARY KEY,
  at           TIMESTAMPTZ NOT NULL,
  event_ids_at BIGINT[] NOT NULL DEFAULT '{}'
);
```

Additive upgrade (`SCHEMA_VERSION = "4"`, `_ADDITIVE_UPGRADES` gains the new
edges). Same watermark semantics as the CLI's file cursors (`cursors.py`
stays for local use): timestamp authoritative, `event_ids_at` tie-breaks,
advance only after the response payload is fully built, `peek` never
advances. The `pulse` tool takes `cursor: str = "default"`, `peek: bool`,
`since: str | None` (bypasses the cursor), `limit`.

**Roles.** `jobhunter_ro` (SELECT everywhere) for agent laptops;
`jobhunter_mcp` = `jobhunter_ro` + INSERT/UPDATE/DELETE on `mcp_cursors`
only. Creation SQL lands in the runbook; Sean runs it (owner action).

## 4. Config

The server reads the same `JOB_HUNTER_*` env as everything else
(`config.py` stays the only env reader). `JOB_HUNTER_ARCHIVE_URL` is set to
the real s3 URL but the serving path never opens the archive (documents'
markdown is in the store); no AWS credentials are provisioned to the
service. New env: `JOB_HUNTER_MCP_TOKEN` (required to start).

## 5. Deploy — Cloud Run

- Dockerfile gains the `mcp` dependency and keeps one image; the Cloud Run
  service overrides the command to `job-hunter-mcp`.
- Service `job-hunter-mcp`, `--min-instances 0 --max-instances 1`,
  `--memory 256Mi`, secrets `JOB_HUNTER_DATABASE_URL` (the `jobhunter_mcp`
  DSN) and `JOB_HUNTER_MCP_TOKEN` via Secret Manager. Region: `us-east4`
  (closest to Neon's us-east; latency is not load-bearing).
- Per Sean's standing rules Claude never runs gcloud writes: the runbook
  gets the exact `gcloud` commands (enable APIs, create secrets, build via
  `gcloud builds submit`, `gcloud run deploy`) for Sean to paste.
- Cost: free tier (2M req/mo) vs ~1k req/mo actual → $0.

## 6. Consumers

- **Committed `.mcp.json`** at the repo root:
  `{"mcpServers": {"job-hunter": {"type": "http", "url": "<cloud-run-url>/mcp", "headers": {"Authorization": "Bearer ${JOB_HUNTER_MCP_TOKEN}"}}}`
  (URL filled after first deploy). Cloud routines check out the repo and load
  it; the cloud environment supplies the token variable.
- **Hourly routine** (created via RemoteTrigger after deploy, not in-repo):
  cron `40 * * * *` UTC — after the :17 CI sync lands. Model sonnet. Slack
  connector attached. Prompt: call `pulse(cursor="hourly")`; empty events +
  clean attention → end quietly; otherwise compose the digest against the
  interest sketch embedded in the routine prompt and Slack-DM Sean. Drill
  down with `profile`/`document` tools only for featured postings.
- The local launchd setup from 2026-09-01 remains a working alternative;
  its file cursor (`hourly`) and the server cursor (`hourly`) are separate
  namespaces — run one, not both, or name them differently.

## 7. Testing

- Parity: for each verb, the CLI path and `views.py` direct call produce
  identical `data` (extends `tests/test_cli_q.py` fixtures).
- MCP integration: FastMCP in-process client against test Postgres — tool
  list, each tool's happy path, limit clamp, truncation flag.
- Auth: missing/wrong/malformed bearer → 401; `/healthz` open.
- Cursor table: advance-after-build, peek, tie-break ids, additive v3→v4
  upgrade path.
- No live Cloud Run test in CI; the runbook ends with a curl smoke
  (`/healthz`, then an authenticated `tools/list`).

## 8. Out of scope

OAuth (static bearer suffices for owner-internal; revisit if the connector
UI path is wanted), public serving (still snippets+attribution, still
later), write verbs of any kind, the L3 linker, claude.ai chat connector
registration (needs OAuth or public URL — follow-up).
