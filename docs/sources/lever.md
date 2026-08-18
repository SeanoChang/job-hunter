# Lever — postings API

Fetched live 2026-08-08 from the `palantir` board: **305 postings, 5.9MB**.
(Also probed `plaid`: HTTP 200 with `[]` — a stale board returns an empty array,
not an error. A registry validator must treat "200 but empty" as suspicious.)

## Endpoint

```
GET https://api.lever.co/v0/postings/{site}?mode=json
```

- No auth. Optional `skip`/`limit` params exist but the full board comes back in
  one call at company scale.
- Without `mode=json` the endpoint returns HTML.

## Envelope

**Bare JSON array** — no wrapper object, no total, no API version.

## Example record (real, strings trimmed)

```json
{
  "id": "ac978161-6f46-4f6b-ad9e-a258e642751c",
  "text": "Administrative Business Partner",
  "categories": {
    "commitment": "Full-time",
    "location": "London, United Kingdom",
    "team": "Administrative",
    "allLocations": ["London, United Kingdom"]
  },
  "country": "GB",
  "workplaceType": "hybrid",
  "createdAt": 1711403416463,
  "opening": "<div><strong><span style=\"font-size: 18px;\">A World-Changing Company</span></strong>…",
  "openingPlain": "A World-Changing Company\n \nPalantir builds the world's leading software…",
  "descriptionBody": "<div><strong style=\"font-size: 18px;\">The Role</strong></div>…",
  "descriptionBodyPlain": "The Role\n \nOur team of Administrative Business Partners…",
  "description": "<div>…opening + body concatenated…</div>",
  "descriptionPlain": "…opening + body concatenated, plain…",
  "additional": "<div><strong><span style=\"font-size: 18px;\">Life at Palantir</span></strong>…",
  "additionalPlain": "Life at Palantir\n \nWe want every Palantirian to achieve…",
  "lists": [
    { "text": "Administrative Business Partner (Foundry)",
      "content": "\n<li>Provide administrative support to a portfolio of individuals/teams…</li>" },
    { "text": "What We Value",
      "content": "\n<li>Ability to adjust quickly, anticipate needs…</li>" }
  ],
  "hostedUrl": "https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c",
  "applyUrl": "https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c/apply"
}
```

## Field notes

| Field | Notes |
|---|---|
| `id` | UUID string (identity: `lever:{site}:{uuid}`) |
| `text` | the job **title** (yes, really) |
| description family | **five overlapping representations**: `opening`, `descriptionBody`, `additional` (each with a `Plain` twin), `description` = opening+body concatenated, and `lists[]` = the bulleted middle sections. Full document ≈ `opening + descriptionBody + lists + additional`. Both HTML and plain are provided — no unescaping needed |
| `categories` | `commitment` (employment type), `team`, `location` + `allLocations[]` |
| `country` | ISO-2 code |
| `workplaceType` | lowercase: `"hybrid"`, `"remote"`, `"onsite"`; some boards omit it |
| `createdAt` | **epoch milliseconds** — the only timestamp; there is **no updated_at**, so change detection is content-diff only |
| compensation | nothing structured on this board; some boards include a `salaryRange` object and/or prose in text — treat as sometimes-present |
| `hostedUrl` / `applyUrl` | canonical posting / apply links |

All fields were 100%-present on this board; cross-company variance
(`salaryRange`, `workplaceType`) needs more boards to confirm.
