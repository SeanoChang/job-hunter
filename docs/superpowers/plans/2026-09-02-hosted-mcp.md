# Hosted MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the CLI's read surface (`pulse` + the seven `q` verbs) over an authenticated streamable-HTTP MCP server that reuses the package's queries verbatim, with server-side cursors (schema v4), packaged for Cloud Run.

**Architecture:** Payload assembly moves out of `cli_q.py` command bodies into pure functions in a new `views.py`; `cli_q.py` and the new `mcp.py` (FastMCP) both call them. Schema v4 adds `mcp_cursors`; a bearer-token ASGI middleware guards everything but `/healthz`.

**Tech Stack:** Python 3.12, uv, `mcp` SDK (FastMCP, new dependency), psycopg 3, pytest (Postgres reachable natively on localhost:5432 — docker is NOT installed, never try it), ruff line 100, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-02-hosted-mcp-design.md` — read first. Also read `src/jobhunter/cli_q.py`, `src/jobhunter/pulse.py`, `src/jobhunter/cursors.py`, and `tests/test_cli_q.py` before Task 1: the refactor must not change any CLI behavior.

## Global Constraints

- mypy strict + ruff clean after every task; full `uv run pytest` green (exit 0) before every commit.
- Env only via `config.py`; the MCP server must not read `os.environ` directly.
- Limits: default 50, hard cap 500, truncation always marked — identical to the CLI.
- The MCP serving path never opens the archive and never writes any table except `mcp_cursors`.
- Branch `mcp/hosted-server`; commit per task with the trailer used throughout this repo:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` + `Claude-Session: https://claude.ai/code/session_01P5FqD7NihBxkuFjggEDWck`. Never push, never touch remotes.
- No behavior change to the CLI in any task — `tests/test_cli.py`, `tests/test_cli_q.py`, `tests/test_pulse.py` must pass unmodified except where a task explicitly says to extend them.

---

### Task 1: `views.py` — extract payload assembly from `cli_q.py`

**Files:**
- Create: `src/jobhunter/views.py`
- Modify: `src/jobhunter/cli_q.py` (bodies call views; flags/emit/fail stay), `src/jobhunter/pulse.py` only if shared helpers move
- Test: `tests/test_views.py` (new), existing `tests/test_cli_q.py` unmodified and green

**Interfaces (produced; Tasks 3–4 consume these exact signatures):**

```python
@dataclass(frozen=True)
class Page:
    data: list[dict[str, Any]] | dict[str, Any]
    truncated: bool = False
    next_cursor: str | None = None

def postings_view(conn, *, source=None, board=None, status=None, since=None,
                  search=None, limit=50, after=None) -> Page
def posting_view(conn, uid: str) -> Page | None            # None = not found
def events_view(conn, *, since=None, kinds=None, source=None, board=None,
                uid=None, limit=50, after_event_id=None) -> Page
def document_view(conn, document_hash: str, *, slice_=None) -> Page | None
def profile_view(conn, document_hash: str, *, full=False) -> Page | None
def claims_view(conn, *, mention, importance=None, source=None, board=None,
                limit=50) -> Page
def boards_view(conn, *, unhealthy_only=False) -> Page
def pulse_view(conn, settings, *, wm: Watermark | None, since_iso: str | None,
               limit: int, boards=None, now: datetime) -> tuple[Page, Watermark | None]
```

Rules: copy the assembly logic (dict shaping, `iso()` conversion, `closed_between`, profile summaries, cursor construction) out of the `cli_q.py` bodies and `pulse.build_pulse` verbatim — this is a move, not a rewrite. `cli_q.py` commands become: parse/validate flags → open conn → call view → `emit`. Validation that produces `fail(...)` (bad status, bad cursor shape, unknown `--fields`) STAYS in `cli_q.py` — views raise `ValueError` and never import typer or cli_output.

- [x] **Step 1:** Write `tests/test_views.py`: for each view, call it directly against the `qenv`-style fixture corpus (reuse/import the fixture setup from `tests/test_cli_q.py`) and assert the returned `Page.data` equals what the corresponding CLI invocation's envelope `data` contains (parity test — invoke the CLI with CliRunner in the same test and compare). Run: expect ImportError.
- [x] **Step 2:** Create `views.py` by moving code; rewire `cli_q.py` and the `pulse` command.
- [x] **Step 3:** `uv run pytest -q` (exit 0 — including untouched test_cli_q.py) `&& uv run ruff check . && uv run mypy`.
- [x] **Step 4:** Commit: `refactor(cli): payload assembly moves to views.py; CLI behavior unchanged`

---

### Task 2: Schema v4 — `mcp_cursors` + store accessors

**Files:**
- Modify: `src/jobhunter/store/schema.sql`, `src/jobhunter/store/db.py`
- Create: `src/jobhunter/store/mcp_state.py`
- Test: `tests/store/test_mcp_state.py`

**Interfaces (produced):**

```python
# store/mcp_state.py — the ONLY writer of mcp_cursors
def read_cursor(conn, name: str) -> Watermark | None
def write_cursor(conn, name: str, wm: Watermark) -> None   # UPSERT; caller commits
```

DDL appended to `schema.sql`:

```sql
-- server-side pulse watermarks for the MCP wrapper (spec 2026-09-02 §3);
-- writer: store/mcp_state.py under the jobhunter_mcp role
CREATE TABLE IF NOT EXISTS mcp_cursors (
  name         TEXT PRIMARY KEY,
  at           TIMESTAMPTZ NOT NULL,
  event_ids_at BIGINT[] NOT NULL DEFAULT '{}'
);
```

`db.py`: `SCHEMA_VERSION = "4"`; `_ADDITIVE_UPGRADES = {("1","2"),("2","3"),("1","3"),("3","4"),("2","4"),("1","4")}`.

- [x] **Step 1:** Failing tests: round-trip through a real Postgres conn (existing `pg` fixture); unknown name → None; upsert overwrites; `Watermark.event_ids_at` tuple survives the BIGINT[] round-trip; the ("3","4") additive upgrade passes `db.init` (pattern: existing upgrade test in `tests/store/test_db.py`).
- [x] **Step 2:** Implement. **Step 3:** Full gates green. **Step 4:** Commit: `feat(store): schema v4 — mcp_cursors watermarks`

---

### Task 3: `mcp.py` — FastMCP app, auth, read tools

**Files:**
- Modify: `pyproject.toml` (add `mcp` to dependencies; console script `job-hunter-mcp = "jobhunter.mcp:main"`), `src/jobhunter/config.py` (add `mcp_token: str | None` from `JOB_HUNTER_MCP_TOKEN`)
- Create: `src/jobhunter/mcp.py`
- Test: `tests/test_mcp.py`

**Interfaces:** FastMCP streamable-HTTP app; tools `postings`, `posting`, `events`, `document`, `profile`, `claims`, `boards` — parameters mirror the corresponding view kwargs (strings/ints only; `since` accepts the CLI's `Nm/Nh/Nd` or ISO), return `{"data": ..., "truncated": ..., "next_cursor": ...}`. Each tool opens a fresh conn via `config.Settings.load()` + `store.db.connect` and closes it (serverless-friendly; no pool in v1). Not-found → a structured MCP tool error (`ToolError`) with the same teaching message the CLI uses. Auth: pure ASGI middleware wrapping the FastMCP app:

```python
class BearerAuth:
    def __init__(self, app: Any, token: str) -> None: ...
    async def __call__(self, scope, receive, send):
        # /healthz passes; otherwise compare header via hmac.compare_digest,
        # else 401 {"error": "unauthorized"}
```

`main()` reads settings, refuses to start without `mcp_token` (exit 3, message names the env var), serves on `0.0.0.0:$PORT` (default 8080) via uvicorn (comes with the `mcp` SDK's dependencies; if not, add it).

- [x] **Step 1:** Failing tests: in-process ASGI client (httpx ASGITransport): `/healthz` open; any tool route without/with-wrong bearer → 401; with the right bearer, MCP `tools/list` names all seven (+pulse in Task 4 — assert ≥7 here); `postings` tool against the fixture corpus returns the same `data` as `views.postings_view` (parity); `limit=9999` clamps to 500.
- [x] **Step 2:** Implement. **Step 3:** Gates green. **Step 4:** Commit: `feat(mcp): streamable-HTTP server — bearer auth, seven read tools`

---

### Task 4: `pulse` tool with server cursors

**Files:**
- Modify: `src/jobhunter/mcp.py`
- Test: `tests/test_mcp.py` (append)

Tool `pulse(cursor: str = "default", peek: bool = False, since: str | None = None, limit: int = 200, boards: str | None = None)`: read watermark via `mcp_state.read_cursor` (ignored when `since` given) → `views.pulse_view` → if not peek and no `since` and a new watermark returned: `mcp_state.write_cursor` + commit AFTER the payload dict is fully built. Returns the pulse payload plus `{"cursor": name, "first_run": ...}`.

- [x] **Step 1:** Failing tests: first call reports the fixture corpus's events with `first_run`; second call returns empty events; `peek=True` twice returns identical events; watermark row visible in `mcp_cursors`; a `since` call neither reads nor writes the table.
- [x] **Step 2:** Implement. **Step 3:** Gates. **Step 4:** Commit: `feat(mcp): pulse tool — server-side watermarks`

---

### Task 5: Packaging — Dockerfile, `.mcp.json`, smoke

**Files:**
- Modify: `Dockerfile` (ensure `uv sync` picks up the new dependency; image must run `job-hunter-mcp` when given as command — no second image), `.dockerignore` if needed
- Create: `.mcp.json` (repo root)
- Test: `tests/test_mcp.py` (append a `main()`-refuses-without-token test); Dockerfile is smoke-checked in Task 7, not built here (no docker locally)

`.mcp.json`:

```json
{
  "mcpServers": {
    "job-hunter": {
      "type": "http",
      "url": "https://REPLACE-AFTER-DEPLOY.run.app/mcp",
      "headers": {"Authorization": "Bearer ${JOB_HUNTER_MCP_TOKEN}"}
    }
  }
}
```

- [x] Steps: failing test for token-less `main()` (SystemExit/typer.Exit code 3) → implement → gates → commit: `feat(mcp): packaging — console script, image command, .mcp.json`

---

### Task 6: Terraform deploy config + runbook + docs sweep

**Files:**
- Create: `infra/main.tf`, `infra/variables.tf`, `infra/outputs.tf`, `infra/README.md`, `docs/runbooks/2026-09-02-deploy-mcp.md`
- Modify: `.gitignore` (add `infra/.terraform/`, `infra/*.tfstate*`, `infra/*.tfvars`), `CLAUDE.md`, `src/jobhunter/CLAUDE.md`, `src/jobhunter/store/CLAUDE.md`, `docs/README.md`, `README.md`

**Terraform (owner-run; Sean applies — Claude never runs terraform apply):**
- Provider `google` (~> 6.x), variables: `project_id`, `region` (default `us-east4`), `image` (full Artifact Registry ref), `service_name` (default `job-hunter-mcp`).
- Resources:
  - `google_project_service` for `run.googleapis.com`, `secretmanager.googleapis.com`, `artifactregistry.googleapis.com`, `cloudbuild.googleapis.com` (`disable_on_destroy = false`).
  - `google_artifact_registry_repository` `job-hunter` (format DOCKER).
  - `google_secret_manager_secret` ×2: `job-hunter-mcp-database-url`, `job-hunter-mcp-token` — **secrets created empty; no `google_secret_manager_secret_version` in Terraform** (values must never enter tf state in a public repo's workflow). The runbook adds versions via `printf ... | gcloud secrets versions add ... --data-file=-`.
  - `google_service_account` `job-hunter-mcp` + `google_secret_manager_secret_iam_member` (`roles/secretmanager.secretAccessor`) on both secrets.
  - `google_cloud_run_v2_service`: the image, `command = ["job-hunter-mcp"]`, scaling min 0 / max 1, 256Mi / 1 CPU, env `JOB_HUNTER_ARCHIVE_URL` (plain var), `JOB_HUNTER_DATABASE_URL` + `JOB_HUNTER_MCP_TOKEN` from the secrets, service account attached, `ingress = "INGRESS_TRAFFIC_ALL"`.
  - `google_cloud_run_v2_service_iam_member` `roles/run.invoker` for `allUsers` — the app's own bearer auth is the gate; Google IAM stays open by design (the MCP client can't do Google IAM).
  - Output: the service URI.
- terraform/tofu is NOT installed on this machine: do not attempt `terraform validate`. Write the config carefully, review it line by line against the provider docs (fetch them if unsure), and say in the commit body that validation runs on Sean's machine.

**Runbook** (`docs/runbooks/2026-09-02-deploy-mcp.md`), copy-pasteable, "every command here is for Sean" at the top: roles SQL (`jobhunter_ro` if absent; `jobhunter_mcp` = ro + `GRANT INSERT, UPDATE, DELETE ON mcp_cursors TO jobhunter_mcp;` — table must exist first via `db init` as owner) → image build (`gcloud builds submit --tag <region>-docker.pkg.dev/<project>/job-hunter/mcp:v1`) → `terraform -chdir=infra init && terraform -chdir=infra plan` → `apply` → add the two secret versions → fill `.mcp.json`'s URL from the terraform output → curl smoke (`/healthz`, then authenticated `tools/list` JSON-RPC).

- [x] Steps: write tf + runbook → `grep` docs for stale claims (schema v3, "no MCP") → gates (run the test suite anyway) → commit: `feat(infra): terraform for the Cloud Run MCP service + deploy runbook`

---

### Task 7: Verification + PR prep

- [x] **Hermeticity fix** (open finding from PR #6, confirmed by Task 1): the test
  suite must not read the developer's real `./.env` or `~/.config/job-hunter/env`.
  Add an autouse fixture in `tests/conftest.py` that monkeypatches
  `XDG_CONFIG_HOME` to a per-session empty tmp dir and chdirs tests away from any
  `.env` (or sets an explicit override env var if that is cleaner given
  `config.load_env_files`'s shape). TDD: a test that creates a poisoned
  `~/.config/job-hunter/env` under a fake `$HOME` and asserts `Settings.load()`
  inside the suite never sees it. Commit separately:
  `fix(tests): suite is hermetic against real user config files`.
- [x] Full `uv run pytest` (exit 0) + ruff + mypy; paste tails into the commit body of any final fix.
- [x] Local live smoke without docker: `JOB_HUNTER_MCP_TOKEN=t JOB_HUNTER_ARCHIVE_URL=file:///tmp/x JOB_HUNTER_DATABASE_URL=<local test DSN> uv run job-hunter-mcp &` then curl `/healthz` and an authenticated `tools/list`; kill it. Record output.
- [x] Tick all plan checkboxes; do NOT push (the driver session pushes and opens the PR).

**Verification record** (2026-09-02, local Postgres, no docker):

- `uv run pytest` → `547 passed`; `uv run ruff check .` → `All checks passed!`;
  `uv run mypy` → `Success: no issues found in 52 source files`. Green as well
  with `JOB_HUNTER_ARCHIVE_URL`, `JOB_HUNTER_DROP_RATIO` and
  `AWS_ACCESS_KEY_ID` exported — the hermeticity fix holds.
- Live smoke: `job-hunter-mcp` on `PORT=8765` against the local corpus.
  `GET /healthz` → `200 {"ok": true, "version": "0.1.0"}`; `tools/list` with no
  bearer and with a wrong one → `401 {"error": "unauthorized"}`; with the
  bearer → `200`, tools `boards, claims, document, events, posting, postings,
  profile, pulse`; `tools/call boards` → the board rows, `isError: false`.
  Process killed, port closed.
