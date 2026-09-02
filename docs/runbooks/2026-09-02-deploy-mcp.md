---
title: Deploy the hosted MCP server — Cloud Run, Terraform, Secret Manager
date: 2026-09-02
type: runbook
status: current
---

# Deploy the hosted MCP server

**Every command here is for Sean.** They create cloud infrastructure, write
secret values and grant database privileges; the assistant writes the config in
`infra/` and stops there (standing rule — and `terraform` is not installed on
the machine it works from, so this config is first validated here).

Why any of this exists: `docs/superpowers/specs/2026-09-02-hosted-mcp-design.md`.
Short version — a cloud routine runs in an ephemeral VM with proxy-restricted
egress, so it cannot keep a cursor file and cannot open a Postgres socket; an
authenticated HTTPS MCP endpoint is the supported path.

Prerequisites: `gcloud` authenticated on the target project with owner or
equivalent, `terraform` ≥ 1.5, the Neon **owner** DSN, and this repository
checked out.

## 1. Database — schema v4, then the two roles

The server keeps its pulse watermarks in `mcp_cursors`, added by schema v4. The
table has to exist before anyone can be granted anything on it, and only the
owner may create it:

```bash
export JOB_HUNTER_DATABASE_URL='postgresql://<owner>:…@…/jobhunter?sslmode=require'
uv run job-hunter db init        # additive 3 → 4; creates mcp_cursors
uv run job-hunter db version     # code and database both say 4
```

Then in `psql` as the owner:

```sql
-- The read role agent machines already use (2026-08-18 runbook). Skip only the
-- CREATE ROLE if it already exists — but ALWAYS re-run the GRANT SELECT below:
-- mcp_cursors is a new (schema v4) table, and a role granted SELECT before it
-- existed does not automatically hold SELECT on it, so pulse would get
-- "permission denied for table mcp_cursors" on its first read.
CREATE ROLE jobhunter_ro LOGIN PASSWORD '…';
GRANT USAGE ON SCHEMA jobhunter TO jobhunter_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA jobhunter TO jobhunter_ro;  -- re-run even if the role pre-existed
ALTER DEFAULT PRIVILEGES IN SCHEMA jobhunter GRANT SELECT ON TABLES TO jobhunter_ro;

-- The server's role: everything jobhunter_ro reads, plus its own cursor rows.
CREATE ROLE jobhunter_mcp LOGIN PASSWORD '…';
GRANT jobhunter_ro TO jobhunter_mcp;
GRANT INSERT, UPDATE, DELETE ON jobhunter.mcp_cursors TO jobhunter_mcp;
```

`mcp_cursors` is the only table the serving path writes, and `pulse` is the only
tool that touches it — everything else is `SELECT`. A confused loop on the
hosted side can lose a reading position and nothing more.

Two things to keep in mind:

- The DSN handed to Cloud Run must be Neon's **direct** connection string, the
  host *without* the `-pooler` suffix. `db.connect` sets `search_path` once per
  session; a transaction-pooled endpoint does not preserve it and every query
  would fail on a missing relation. Same rule as the fetcher's DSN.
- A `rebuild` replays the archive into a fresh schema and renames it over the
  live one. It replays these grants (`db.capture_grants`) and carries the
  watermark rows across (`mcp_state.carry_cursors`), so none of the SQL above
  has to be re-run afterwards. The `ALTER DEFAULT PRIVILEGES` line is the
  exception — it is keyed to the schema object, not its name, and goes inert at
  the first rebuild.

## 2. Terraform variables

`infra/` needs a project and an image reference. Put both in
`infra/terraform.tfvars` (gitignored) so no command below repeats them; the
image tag names a build that does not exist yet, which is fine — it is only read
when the service is created, in step 5.

```bash
cat > infra/terraform.tfvars <<'EOF'
project_id = "<project>"
image      = "us-east4-docker.pkg.dev/<project>/job-hunter/mcp:v1"
EOF
terraform -chdir=infra init
```

## 3. First apply — registry and secrets only

The deploy takes two applies. A Cloud Run revision resolves both secret
references before it is allowed to serve, so the secrets must hold a version
before the service is created — and the image must be pushed to a repository
that exists. Both come from this targeted apply (it pulls in the API
enablements they depend on):

```bash
terraform -chdir=infra apply \
  -target=google_artifact_registry_repository.images \
  -target=google_secret_manager_secret.database_url \
  -target=google_secret_manager_secret.mcp_token
```

## 4. Secret values

Terraform declares the secrets empty on purpose: a `secret_version` resource
would put both values in `infra/terraform.tfstate`, a plain file next to a
public git tree. Add them out-of-band instead.

```bash
# The jobhunter_mcp DSN from step 1 — direct host, not the pooler.
printf '%s' 'postgresql://jobhunter_mcp:…@…/jobhunter?sslmode=require' \
  | gcloud secrets versions add job-hunter-mcp-database-url --data-file=-

TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
printf '%s' "$TOKEN" | gcloud secrets versions add job-hunter-mcp-token --data-file=-
```

> [!IMPORTANT] `printf '%s'`, never `echo`
>
> Secret Manager stores exactly the bytes it is given and Cloud Run injects them
> verbatim. `echo` appends a newline, the bearer comparison is
> `hmac.compare_digest` over the whole header value, and every request would
> answer 401 with nothing in the logs to say why.

The same token value goes wherever a client runs — the cloud environment's
variables and the local shell — as `JOB_HUNTER_MCP_TOKEN`; `.mcp.json`
references it by name and never holds it. Read it back from `$TOKEN` in this
shell while it is still set, then `unset TOKEN`.

## 5. Build the image, then apply the rest

One image serves both faces; the Cloud Run command picks `job-hunter-mcp`.

```bash
gcloud builds submit --tag us-east4-docker.pkg.dev/<project>/job-hunter/mcp:v1
terraform -chdir=infra plan          # read it: service, SA, bindings, invoker
terraform -chdir=infra apply
terraform -chdir=infra output
```

## 6. Point clients at it

```bash
terraform -chdir=infra output -raw mcp_url    # https://job-hunter-mcp-….run.app/mcp
```

Paste that into `.mcp.json`'s `url` (it ships with a placeholder host) and
commit. The token stays an environment reference — the repository is public.

## 7. Smoke test

```bash
URL=$(terraform -chdir=infra output -raw service_uri)

curl -sS "$URL/healthz"                      # {"ok": true, "version": "0.1.0"}
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$URL/mcp"   # 401: the gate is on

curl -sS -X POST "$URL/mcp" \
  -H "Authorization: Bearer $JOB_HUNTER_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | jq -r '.result.tools[].name'              # eight: pulse + the seven q verbs
```

`tools/list` proves the transport and the token. One tool call proves the DSN
and the grants, which are the parts step 1 could get wrong:

```bash
curl -sS -X POST "$URL/mcp" \
  -H "Authorization: Bearer $JOB_HUNTER_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"boards","arguments":{}}}' | jq '.result.structuredContent'
```

A `pulse` call is the last one to run, because it advances a real cursor:
`{"name":"pulse","arguments":{"cursor":"smoke","peek":true}}` reports without
advancing anything, and `DELETE FROM mcp_cursors WHERE name = 'smoke';` removes
the row if a non-peek call created one.

## Afterwards

- **Redeploy.** Build a new tag, change `image` in `terraform.tfvars`, apply.
  Traffic moves to the new revision; `gcloud run services update-traffic
  job-hunter-mcp --to-revisions <old>=100` rolls back without Terraform.
- **Cost.** Cloud Run's free tier is 2M requests a month against ~1k actual, and
  the service scales to zero between calls. The first call of an hour pays a
  cold start of a few seconds — nothing an hourly routine notices.
- **Cursor namespaces.** The hosted cursor named `hourly` and the local launchd
  setup's file cursor named `hourly` are different watermarks in different
  places. Run one or the other, or give them different names, or the two will
  each report deltas the other already covered.
- **Teardown.** `terraform -chdir=infra destroy` removes the service, the
  registry and both secrets (`deletion_protection` is off and the APIs stay
  enabled). The database roles are not Terraform's; drop them by hand.
