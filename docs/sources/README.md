# Data sources — real payloads, compared

Live fetches, 2026-08-08. One board per ATS (all public, unauthenticated APIs):

| | Greenhouse (`anthropic`) | Lever (`palantir`) | Ashby (`ramp`) |
|---|---|---|---|
| Postings | 393 | 305 | 122 |
| Payload | 5.7MB | 5.9MB | 2.2MB |
| ≈ per posting | ~14KB | ~19KB | ~18KB |
| Envelope | `{jobs, meta.total}` | bare array | `{apiVersion, jobs}` |
| Posting id | numeric | UUID | UUID |
| Title field | `title` | `text` | `title` |
| Description | HTML-**escaped** string | 5 overlapping HTML+plain fields | clean `descriptionHtml`+`descriptionPlain` pair |
| Update timestamp | `updated_at` ✅ | ❌ (`createdAt` only, epoch ms) | ❌ (`publishedAt` only) |
| Structured comp | ❌ (prose in content) | ❌/sometimes | ✅ tiers + min/max/currency |
| Remote flag | ❌ (infer from text) | `workplaceType` `"hybrid"` | `isRemote` bool + `workplaceType` `"Hybrid"` |
| Company name in payload | ✅ | ❌ | ❌ |

Details + full example records: [greenhouse.md](greenhouse.md) ·
[lever.md](lever.md) · [ashby.md](ashby.md)

Two findings that shape the pipeline:

1. **Only Greenhouse says when a posting changed.** Lever and Ashby give no
   update timestamp, so change detection must be *our* job: diff snapshots by
   content hash. This validates the snapshot-diff design.
2. **Every source disagrees about everything else** — casing (`hybrid` vs
   `Hybrid` vs nothing), timestamps (offset-ISO vs epoch-ms vs UTC-ISO),
   description encoding (escaped vs duplicated vs clean). Normalization is
   where the real work is; fetching is trivial.

## Unified format (draft) — "same shape, tagged with source"

> Revision 2026-08-17: `content_hash` below is split into `raw_capture_hash`,
> `version_hash` (+ hash version over an explicit field list) and a document
> hash of the canonical Markdown; description handling is HTML → Markdown as
> the only canonical text. See `../2026-08-17-parsing-direction.md`.

Every posting from every source normalizes into one record; `source` +
`source_id` is the provenance label, and the untouched original rides along in
`raw` so nothing is ever lost:

```jsonc
{
  "source":            "greenhouse | lever | ashby",
  "board":             "anthropic",              // registry key
  "source_id":         "5101378008",             // stringified original id
  "title":             "Account Executive",      // trimmed
  "company":           "Anthropic",              // from payload (GH) or registry (Lever/Ashby)
  "locations":         ["Sydney, Australia"],    // primary + secondaries, flattened
  "workplace_type":    "remote | hybrid | onsite | null",  // casing normalized
  "is_remote":         true,                     // explicit (Ashby) or inferred
  "department":        "Sales",                  // GH departments[0] / Lever team / Ashby department
  "team":              null,                     // finer grain where available
  "employment_type":   "full_time | ... | null",
  "compensation":      { "min": 211400, "max": 290600,
                         "currency": "USD", "interval": "year" },  // null unless structured
  "url":               "https://…",
  "apply_url":         "https://…",
  "source_created_at": "2026-04-07T20:10:24Z",   // all timestamps → UTC ISO
  "source_updated_at": "2026-08-03T22:25:22Z",   // null for Lever/Ashby
  "description_html":  "<div>…unescaped, single document…</div>",
  "description_text":  "…plain text…",
  "raw":               { /* original record, verbatim */ }
}
```

Per-source mapping:

| Unified | Greenhouse | Lever | Ashby |
|---|---|---|---|
| `source_id` | `id` (numeric→str) | `id` | `id` |
| `title` | `title` | `text` | `title` (trim!) |
| `company` | `company_name` | registry | registry |
| `locations` | `location.name` + `offices[].location` | `categories.allLocations` | `location` + `secondaryLocations[].location` |
| `workplace_type` | — | `workplaceType` ↓case | `workplaceType` ↓case |
| `is_remote` | infer | `workplaceType=="remote"` | `isRemote` |
| `department` | `departments[0].name` | `categories.team` | `department` |
| `team` | — | — | `team` |
| `employment_type` | — | `categories.commitment` | `employmentType` |
| `compensation` | — (prose) | — (sometimes) | `compensation.summaryComponents` (Salary row) |
| `source_created_at` | `first_published` | `createdAt` (epoch ms) | `publishedAt` |
| `source_updated_at` | `updated_at` | — | — |
| `description_html` | unescape(`content`) | `opening`+`descriptionBody`+`lists`+`additional` | `descriptionHtml` |
| `description_text` | strip tags | corresponding `*Plain` fields | `descriptionPlain` |

Observation times (`first_seen` / `last_seen` / `closed_at`) are deliberately
**not** here — they belong to the lake/store layer, since they describe what
*we* observed, not what the source claims. They're also the only timestamps
comparable across sources.

## Scale of the data

Grounded in today's numbers (~15KB per posting raw, HTML gzips ~10:1):

**Personal scale** (registry of ~100 hand-picked companies):
~20–30k open postings; a full raw snapshot ≈ 300–450MB uncompressed, ~30–45MB
gzipped. Naive daily snapshots ≈ 12–16GB/year compressed — fine on a laptop.
Since only a few percent of a board changes per day, storing unchanged payloads
once (content-hash dedup) cuts that ~10–20×: **~1GB/year**. History is cheap;
there is no storage excuse to skip it.

**Universal scale** (every discoverable public board — stage 3 territory):
tens of thousands of boards across the three ATSs, a few million open postings
at any moment (for reference, US total job openings run ~7–8M per BLS JOLTS —
ATS-public postings are a meaningful, measurable slice). Full raw snapshot
≈ 40–50GB/day uncompressed; with change-only storage, on the order of **a few
GB/day compressed** — object-store territory, still not exotic.

**Trend/analysis data is tiny by comparison.** What labor-market analysis needs
is the *event stream* (posting opened / changed / closed, with extracted fields
like title, location, comp) at a few hundred bytes per event — a year of even
universal-scale events fits in single-digit GB. The raw payload archive is the
bulky, irreplaceable part; the analytics layer on top stays light.

## Reproducing the fetches

```sh
curl -o gh.json    "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true"
curl -o lever.json "https://api.lever.co/v0/postings/palantir?mode=json"
curl -o ashby.json "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"
```
