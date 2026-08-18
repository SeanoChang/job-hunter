# Greenhouse — job board API

Fetched live 2026-08-08 from the `anthropic` board: **393 postings, 5.7MB** with content.

## Endpoint

```
GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
```

- No auth, no pagination — one call returns the whole board.
- Without `?content=true` the response drops descriptions/departments/offices
  (281KB vs 5.7MB for the same board) — always fetch with it.
- Per-job detail also exists: `GET .../jobs/{id}` (adds application questions).

## Envelope

```json
{ "jobs": [ ... ], "meta": { "total": 393 } }
```

## Example record (real, strings trimmed)

```json
{
  "id": 5101378008,
  "internal_job_id": 4418623008,
  "requisition_id": "260322",
  "title": "Account Executive, Public Sector",
  "company_name": "Anthropic",
  "absolute_url": "https://job-boards.greenhouse.io/anthropic/jobs/5101378008",
  "location": { "name": "Sydney, Australia" },
  "offices": [
    { "id": 4053894008, "name": "Sydney, Australia",
      "location": "Sydney, New South Wales, Australia",
      "child_ids": [], "parent_id": null }
  ],
  "departments": [
    { "id": 4002062008, "name": "Sales", "child_ids": [], "parent_id": null }
  ],
  "metadata": [
    { "id": 4036944008, "name": "Location Type", "value": null,
      "value_type": "single_select" }
  ],
  "data_compliance": [
    { "type": "gdpr", "requires_consent": false,
      "requires_processing_consent": false,
      "requires_retention_consent": false, "retention_period": null,
      "demographic_data_consent_applies": false }
  ],
  "first_published": "2026-04-07T16:10:24-04:00",
  "updated_at": "2026-08-03T18:25:22-04:00",
  "language": "en",
  "application_deadline": null,
  "content": "&lt;div class=&quot;content-intro&quot;&gt;&lt;h2&gt;&lt;strong&gt;About Anthropic&lt;/strong&gt;…",
  "ai_disclaimer": null, "include_ai_disclaimer": null,
  "ai_opt_out_request_url": null
}
```

## Field notes

| Field | Notes |
|---|---|
| `id` / `internal_job_id` | numeric; `id` is the public posting id (identity: `greenhouse:{board}:{id}`) |
| `title` | plain string |
| `company_name` | present on every job — the only source that self-identifies the company |
| `content` | **HTML-escaped** HTML (`&lt;div&gt;…`) — must entity-unescape *then* parse; US pay-transparency salary ranges usually live in here as prose |
| `location.name` | one free-text string; multi-location jobs repeat as separate postings or list in `offices` |
| `offices` / `departments` | structured, hierarchical (`parent_id`/`child_ids`) |
| `metadata` | company-defined custom fields — schema varies per company, values often null |
| `updated_at` | **the only ATS of the three exposing an update timestamp** — offset-style ISO (`-04:00`) |
| `first_published` | posting's publish date (offset-style ISO) |
| compensation | no structured field at board level — extraction from `content` needed |
| remote-ness | no flag — infer from location text ("Remote-friendly…", etc.) |

Presence was 100% for every field across all 393 postings on this board; other
companies' `metadata` will differ (custom fields are per-company).
