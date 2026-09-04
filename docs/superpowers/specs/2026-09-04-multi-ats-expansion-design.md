# Multi-ATS expansion: two-phase sources (Workday, Oracle HCM, Amazon, SmartRecruiters, Eightfold)

**Status:** design for review · **Author:** session 2026-09-04 · **Depends on:**
`docs/2026-08-18-ingestion-layer-spec.md` (normative), top-100 coverage matrix
(claude.ai artifact 897bd75e, raw probe data archived in session scratchpad).

## 1. Goal and non-goals

Bring the top-100 SWE employers into the corpus. The coverage research (147
companies, live-verified endpoints) splits the gap into four adapter families:

| family | companies | postings (verified 2026-09-04) |
|---|---|---|
| Workday CXS | 26 (NVIDIA, Salesforce, Capital One, Morgan Stanley, Adobe, Intel, …) | ~19,000 |
| Oracle Recruiting Cloud | 6 (JPMC 7,317, Oracle, TI, Uber, Amex, Akamai) | ~11,450 |
| Amazon jobs JSON | 1 | 10,000+ (display cap) |
| SmartRecruiters + Eightfold | 5 (Grab, Canva, Wix, Snap; Netflix) | ~1,200 |

Non-goals: the blocked tier (Google, Meta, Apple, Tesla, banks with bot
challenges — needs a browser-assisted endpoint hunt, separate effort); HTML
scraping; anything requiring auth or challenge bypass.

## 2. Policy change (explicit)

The founding rule "official ATS APIs, no scraping" is relaxed to:

> Postings are ingested from structured JSON endpoints only: official ATS APIs
> first, and otherwise the first-party JSON endpoint the company's own careers
> page calls. Never HTML scraping, never authentication, never bypassing bot
> challenges or rate limits. Every request carries an honest User-Agent;
> per-board budgets and backoff keep load below what a human browsing the site
> would generate. A 403/challenge marks the board `blocked` — it is never
> retried around.

README and root CLAUDE.md get this wording; the ingestion spec gains it as an
amendment ruling.

## 3. The architectural change: two-phase sources

### 3.1 Why the current protocol doesn't fit

`sources/base.Source` is `url(board) → parse(body) → normalize(rec)`: one
request returns every posting with its full description. Workday's CXS list
returns 20 rows/page with **no description** and **no content hash**; the
description costs one detail request per posting. NVIDIA alone is 100 list
pages + up to 2,000 details — a full detail sweep per hourly run is out of the
question. Oracle HCM, Amazon, and Eightfold have the same shape.

### 3.2 `TwoPhaseSource` protocol (adapters stay pure, I/O stays in fetch.py)

```python
class TwoPhaseSource(Protocol):
    name: str                    # "workday", "oraclehcm", ...
    adapter_version: str

    def list_url(self, board: Board, offset: int) -> Request: ...
        # Request = (url, method, body|None) — POST for Workday CXS
    def parse_list(self, body: bytes) -> ListPage: ...
        # ListPage = (rows: tuple[ListRow, ...], total: int)
        # ListRow = (uid, detail_path, title, coarse fields, raw payload)
    def detail_url(self, board: Board, row: ListRow) -> Request: ...
    def normalize_detail(self, body: bytes, row: ListRow,
                         board: Board) -> PostingVersion: ...
```

`fetch.py` drives the loop: page the list until `total` is covered (hard page
cap per board), then fetch details for the budgeted subset (§3.4). Adapters
never do I/O — same testing story as today (recorded fixtures through
`parse_list`/`normalize_detail`).

`http.py` grows two knobs used only by paged loops: `spacing_ms` between
requests to the same host (default 250) and per-request `method`/`json_body`
support (today it is GET-only).

### 3.3 Archive shape (archive stays truth, rebuild stays possible)

One `AttemptManifest` per board per run, as today, extended with two optional
fields (additive, old manifests remain valid):

- `page_blobs: [sha256, …]` — raw bytes of every list page, in order. The
  concatenation of pages is the presence snapshot: a uid in any page is open.
  `blob_sha256` stays null for two-phase boards (there is no single body);
  `record_count` = total list rows.
- `details: [{uid, blob_sha256, http_status, error}]` — one entry per detail
  fetched **this attempt** (raw bytes archived individually,
  content-addressed, before any store write — archive-first is unchanged).

Rebuild: presence replays from `page_blobs`; versions/documents replay from
every manifest's `details` entries (idempotent on content hash, exactly like
today's replay).

### 3.4 Detail budget and freshness

Per board, per run:

1. **New first:** uids present in the list with no archived detail yet,
   newest-listed first, up to `detail_budget` (default 40/board/run).
2. **Staleness sweep:** remaining budget re-fetches details whose last fetch is
   older than `redetail_days` (default 7), oldest first. A changed body hashes
   to a new version through the normal `hashing.py` path; an identical body is
   a no-op.

Consequences, stated honestly:

- A new board backfills at ~960 details/day (hourly cron): NVIDIA (~2,000) is
  complete in ~2 days, the whole Workday wave in under a week.
- Edits are eventually consistent (≤ `redetail_days` late). Presence — the
  open/closed signal — is **not** delayed: it comes from the list every run.
- Until its detail lands, a posting exists from the list row alone: it enters
  `postings` with title/locations from the list, `current_version_hash` NULL,
  presence `parse_status = "pending_detail"`. It joins the L2 queue only when
  a document exists — no downstream change to extraction or the MCP surface.

### 3.5 Registry extension

`[[boards]]` gains per-source keys, validated per source by `registry.py`:

```toml
[[boards]]                                  # Workday
company = "NVIDIA"
source  = "workday"
board   = "nvidia"                          # tenant
host    = "wd5"                             # wd1|wd2|wd3|wd5|wd12|wd108|…
site    = "NVIDIAExternalCareerSite"

[[boards]]                                  # Oracle Recruiting Cloud
company = "JPMorgan Chase"
source  = "oraclehcm"
board   = "jpmc"
base    = "https://jpmc.fa.oraclecloud.com"
site    = "CX_1001"

[[boards]]                                  # SmartRecruiters (one-shot, official API)
company = "Canva"
source  = "smartrecruiters"
board   = "canva"

[[boards]]                                  # Eightfold
company = "Netflix"
source  = "eightfold"
board   = "netflix"
base    = "https://explore.jobs.netflix.net"
domain  = "netflix.com"

[[boards]]                                  # Amazon (single fixed board)
company = "Amazon"
source  = "amazonjobs"
board   = "amazon"
```

Unknown keys stay errors; `registry check` verifies the per-source required
set. `Board` model gains an `extra: Mapping[str, str]` (frozen) carrying them.

## 4. The five adapters

### 4.1 workday (two-phase) — unlocks 26 companies

- List: `POST https://{board}.{host}.myworkdayjobs.com/wday/cxs/{board}/{site}/jobs`
  body `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}` →
  `{total, jobPostings:[{title, externalPath, locationsText, postedOn,
  bulletFields}]}`. uid = `bulletFields[0]` (req id) with `externalPath` as
  fallback; page cap 250 pages.
- Detail: `GET …/wday/cxs/{board}/{site}{externalPath}` → `jobPostingInfo`
  with `jobDescription` (HTML → `description_html`), `timeType`, `location` +
  `additionalLocations`, `postedOn`, `externalUrl`.
- No structured compensation field; ranges appear in the description text
  (verified on NVIDIA), which L2 already handles.
- Splunk special case: Cisco tenant with `searchText=splunk` — deferred; Splunk
  ships when Cisco's own board does, not as a filtered pseudo-board.

### 4.2 oraclehcm (two-phase) — unlocks 6 companies

- List: `GET {base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&finder=findReqs;siteNumber={site},limit=200,offset=N`
  → `items[0].requisitionList[]` (+ `TotalJobsCount`).
- Detail: same resource with the `ById` finder
  (`finder=ById;Id="{reqId}",siteNumber={site}` + `expand=all`) → full
  description fields. Exact finder syntax is pinned during fixture capture —
  the list endpoint is verified live; the detail variant is the one unverified
  link in this family.

### 4.3 amazonjobs (two-phase, may degrade to one-shot)

- List: `GET https://www.amazon.jobs/en/search.json?result_limit=100&offset=N`
  (verified). Fixture capture decides whether `description`/qualification
  fields in the search response are complete enough to skip the detail call;
  if not, detail = the posting's `/en/jobs/{id_icims}.json`. 10k display cap
  accepted — facet-partitioned sweeps (by category/location) are a later
  increment if coverage proves short.

### 4.4 smartrecruiters (two-phase, official documented API)

- List: `GET https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100&offset=N`.
- Detail: `GET …/postings/{id}` → `jobAd.sections` HTML.

### 4.5 eightfold (two-phase)

- List: `GET {base}/api/apply/v2/jobs?domain={domain}&start=N&num=10` →
  `positions[]` (+ count). Detail included per position (`job_description`);
  fixture capture confirms — if complete, Eightfold runs list-only with
  synthetic details from list rows.

## 5. Scale, cost, and storage

- Fetch load: ~1,100 extra list requests/hour across all new boards (NVIDIA
  100, most boards ≤ 25) + ≤ 40 details/board/run, threaded as today with
  250 ms same-host spacing. Fetch step grows from ~3 to an estimated 6–9 min —
  still inside the budget after the runner wall-clock work.
- L2: +42k documents ≈ **$300 one-off** at Luna prices (hourly cap currently
  840 docs/day → ~7 weeks to drain; acceptable, or raised later by the Cloud
  Run extraction worker). Steady-state inflow roughly doubles → ~$4–6/day.
- Neon: markdown for +42k docs ≈ +250 MB — past the 0.5 GB free tier.
  **Launch upgrade is a prerequisite for the backfill** (~$3–6/mo at this
  size). R2 growth is negligible in cost.
- New-board flood control: boards are added in waves (§6) so `pulse` and the
  daily digest aren't buried under 40k "new posting" events in one day;
  first-seen events from a board's first week are digest-summarized, not
  itemized (existing attention-block behavior; verified during Wave W1 canary).

## 6. Integration plan (phases, each independently shippable)

Every phase follows the same gate: recorded fixtures → unit tests (mirroring
`tests/sources/`) → `registry check` → opt-in live smoke → one-board fetch
canary via `workflow_dispatch` → wave the remaining boards in → extraction
canary → profile spot-check via dump-profiles.

- **Phase W0 — machinery (the architectural core, no new boards).**
  `TwoPhaseSource` protocol, fetch.py two-phase driver, http.py POST/JSON-body
  + spacing, manifest `page_blobs`/`details` fields, ingest/rebuild handling of
  `pending_detail`, registry `extra` keys. Ships dark: no behavior change for
  existing boards, proven by the untouched existing test suite.
- **Phase W1 — Workday adapter + NVIDIA.** Adapter + fixtures; registry adds
  `workday:nvidia` alone; canary; then verify presence, versions, L2 profiles
  on real NVIDIA postings (the "minimum/preferred qualifications" parse check).
- **Phase W2 — Workday wave.** Add the remaining 25 verified tenants in two
  registry PRs (majors first). Dell's site slug and Booking's oddly small
  board (22) are re-verified at add time.
- **Phase O — Oracle HCM adapter + 6 boards.** JPMC first (largest single
  unlock in the whole plan).
- **Phase A — Amazon adapter.**
- **Phase S — SmartRecruiters + Eightfold adapters + 5 boards.**
- **Phase X (separate, later) — blocked tier.** Browser-assisted endpoint
  discovery for Google/Meta/Apple/Tesla/banks, spec'd only after W–S land.

Sequencing rationale: W0+W1 carry all architectural risk on one board;
everything after is per-family adapter work on a proven chassis. O before A
because JPMC alone outweighs Amazon's marginal value to this corpus.

## 7. Risks and mitigations

- **Unofficial endpoints drift** (Qualcomm and Grab moved off Workday
  mid-2026): envelope errors are already loud per board in `status`/CI; a
  board erroring for 7 days gets flagged by the existing health surface.
  Adapter contract changes are adapter-version bumps, archived as such.
- **Rate-limiting / blocking:** budgets + spacing + honest UA; on 403 or a
  challenge page the board goes `blocked` in the manifest and is skipped, not
  retried around (policy §2).
- **Coarse `postedOn` (no list-side change signal):** accepted — edits are
  ≤ 7 days late; presence is exact.
- **Manifest field creep:** both new fields optional; `is_healthy` and replay
  treat their absence exactly as today (backward compatible by construction).
- **Unverified links called out:** Oracle detail finder syntax; Amazon
  description completeness; Eightfold detail completeness. Each is pinned by
  the fixture-capture step of its phase before any store code depends on it.

## 8. Open questions for review

1. Wave W2 adds ~19k postings against a 0.5 GB Neon free tier — confirm the
   Launch upgrade before W2 (W1/NVIDIA alone fits).
2. `detail_budget=40`/board/run and `redetail_days=7` — tune now or after W1
   telemetry?
3. Splunk-via-Cisco `searchText` pseudo-boards: in or out? (Spec says out.)
4. Amazon's 10k cap: accept, or spec facet-partitioned sweeps in Phase A?
