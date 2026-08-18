# Ashby — posting API

Fetched live 2026-08-08 from the `ramp` board: **122 postings, 2.2MB**.

## Endpoint

```
GET https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true
```

- No auth, no pagination.
- `includeCompensation=true` is opt-in and worth it — Ashby is the only one of
  the three ATSs with **structured compensation data**.

## Envelope

```json
{ "apiVersion": "v0.1", "jobs": [ ... ] }
```

The only source that versions its API in the payload — worth recording.

## Example record (real, strings trimmed)

```json
{
  "id": "34413f8d-26bf-4bbc-8ade-eb309a0e2245",
  "title": " Security Engineer, Cloud",
  "department": "Engineering",
  "team": "Backend",
  "employmentType": "FullTime",
  "location": "New York, NY (HQ)",
  "secondaryLocations": [
    { "location": "Remote (Canada)",
      "address": { "postalAddress": { "addressCountry": "Canada" } } },
    { "location": "Remote (US)",
      "address": { "postalAddress": { "addressCountry": "United States" } } }
  ],
  "address": { "postalAddress": {
    "addressRegion": "NY", "addressCountry": "USA",
    "addressLocality": "New York City" } },
  "isRemote": true,
  "isListed": true,
  "workplaceType": "Hybrid",
  "publishedAt": "2026-04-07T17:12:35.753+00:00",
  "jobUrl": "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245",
  "applyUrl": "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245/application",
  "descriptionHtml": "<h1><strong>About Ramp</strong></h1><p style=\"min-height:1.5em\">Ramp is building…",
  "descriptionPlain": "ABOUT RAMP\n\nRamp is building the smart infrastructure for finance teams…",
  "shouldDisplayCompensationOnJobPostings": true,
  "compensation": {
    "compensationTierSummary": "$211.4K – $290.6K • Offers Equity",
    "scrapeableCompensationSalarySummary": "$211.4K - $290.6K",
    "compensationTiers": [
      { "tierSummary": "$211.4K – $290.6K • Offers Equity",
        "components": [
          { "compensationType": "Salary", "interval": "1 YEAR",
            "currencyCode": "USD", "minValue": 211400, "maxValue": 290600 },
          { "compensationType": "EquityPercentage", "interval": "NONE",
            "currencyCode": null, "minValue": null, "maxValue": null }
        ] }
    ],
    "summaryComponents": [
      { "compensationType": "Salary", "interval": "1 YEAR",
        "currencyCode": "USD", "minValue": 211400, "maxValue": 290600 },
      { "compensationType": "EquityPercentage", "interval": "NONE",
        "currencyCode": null, "minValue": null, "maxValue": null }
    ]
  }
}
```

## Field notes

| Field | Notes |
|---|---|
| `id` | UUID string (identity: `ashby:{board}:{uuid}`) |
| `title` | note the **leading space** in this real record — normalize/trim everything |
| `department` / `team` | flat strings (no hierarchy) |
| `employmentType` | PascalCase enum: `"FullTime"`, … |
| `location` + `secondaryLocations[]` | primary string + structured extras with postal addresses; `isRemote` boolean is explicit |
| `workplaceType` | capitalized: `"Hybrid"` (vs Lever's lowercase `"hybrid"` — normalize casing across sources) |
| `publishedAt` | ISO 8601 UTC with ms; **no updated_at** — change detection is content-diff only |
| `isListed` | filter — unlisted postings can appear in the payload |
| `descriptionHtml` / `descriptionPlain` | clean pair, no unescaping needed |
| `compensation` | fully structured: tiers → components with `minValue`/`maxValue`/`currencyCode`/`interval` — the gold standard for posted-wage analytics |

All fields 100%-present on this board; `compensation` details will vary by
company (only where pay transparency applies).
