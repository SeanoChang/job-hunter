---
title: ATS coverage — what share of jobs our data setup reaches
date: 2026-08-14
type: report
status: current
---

# Coverage: what a Greenhouse/Lever/Ashby fetcher actually reaches

Method: two research agents (ATS market share by segment; aggregator coverage
and JSON-LD adoption), each followed by a citation verifier that fetched every
cited URL and spot-checked counts. 27 references. Technology-tracker counts
drift daily — every number here is date-stamped, and tracker vs vendor vs
live-board-scan biases are stated. The two agents produced overlapping but not
identical postings-weighted estimates; both are shown.

> [!TLDR] Coverage depends entirely on the denominator
>
> For a curated panel of VC-backed tech companies — our actual use case — the
> big-3 public APIs reach roughly 65–80% of companies, and the planned
> adapter/JSON-LD ladder raises reachable companies to roughly 90–95%. For all
> US tech postings (postings-weighted), the big-3 slice is only ~20–45%, because
> enterprise volume lives on Workday/iCIMS/SuccessFactors. And all online
> postings together cover only 22–49% of JOLTS openings — the rest are never
> posted. Conclusion: the design is right for its stated scope (watch a chosen
> panel deeply), wrong to ever describe as "the job market."

## Coverage estimates (August 2026, with uncertainty)

| Population                                | Big-3 APIs | With adapter + JSON-LD ladder |
| ----------------------------------------- | ---------- | ----------------------------- |
| Curated VC-tech panel, share of companies | ~65–80%    | ~90–95%                       |
| Blended panel incl. enterprise, companies | ~50–65%    | ~80–90%                       |
| All US tech postings (postings-weighted)  | ~20–45%    | ~70–85% practical ceiling     |
| All US job openings (incl. never-posted)  | small      | capped by 22–49% online share |

The postings-weighted row is low confidence (±15 points): one agent estimated
30–45%, the other 20–35%; they overlap at 30–35%. The only hard anchor is
Greenhouse's ~175,000 live postings (Fortune, July 2026). The unreachable
remainder after the full ladder (~15–30% of tech postings) is bespoke
unstructured pages plus board-only postings (LinkedIn Easy-Apply-only and
recruiter-posted roles — no reliable measurement exists; likely small for
companies with real career pages).

## The denominators

- JOLTS: 7.359M US job openings, June 2026 preliminary (all industries;
  openings, not postings; verified via the BLS public API).
- LinkUp: up to 10M daily active postings indexed solely from 86,000+ employer
  websites — the best proxy for "postings on some career page."
- HiringCafe live counter (2026-08-14): 3,837,925 jobs across 122,883 companies,
  indexed directly from career pages across ~46 ATS platforms — a live floor for
  what structured career-page fetching can reach, roughly half of JOLTS
  openings.
- Online postings of any kind covered 22–49% of JOLTS openings depending on year
  (Dalton, Kahn & Mueller — carried from the 2026-08-08 labor memo).

## Big-3 size: vendor vs tracker vs live-board numbers

Three kinds of counts disagree by up to 4–5x, systematically:

- **Vendor-stated (lower bound, paying customers):** Greenhouse "more than 7,500
  companies" (PR Newswire, March 2026); Ashby 1,300 → 2,700+ customers at its
  July 2025 Series D, with a 2026 secondary source saying 4,000+; Lever
  publishes nothing post-acquisition (parent Employ cites "6k+" across Lever +
  JazzHR + Jobvite, so Lever alone is plausibly 2–4k).
- **Technology trackers (upper bound, incl. churned/stale installs):** 6sense
  showed Greenhouse ~23.5k, Lever ~8.6k, iCIMS ~12.8k; TheirStack Greenhouse
  ~31.5k, Ashby ~13.9k (all drifting daily — retrieved mid-August 2026).
  TheirStack counts every company ever observed posting; treat as cumulative,
  not current.
- **Live-board scans (the number that matters to us):** Bloomberry detected
  3,596 companies with active Ashby boards on 2026-08-13. This is "fetchable
  today" — the measurement our `add-company` probe effectively reproduces per
  panel.

Big-3 total: roughly 12,000–14,000 companies with live public boards, about 10%
of the companies HiringCafe indexes — but concentration inverts inside VC-backed
tech (next section).

## Segment structure: where the big three do and don't dominate

- **Startups/scaleups:** Pin's July 2026 report (600+ recruiting teams, a
  startup-skewed live-integration sample): Ashby 15.6% overall and 27.0% among
  teams with 6+ recruiting seats; Greenhouse 5.2%; Lever 1.5%. A 2026 census of
  800+ startups (ConnectHum via 100Hires — vendor-directional methodology) found
  335 on Ashby and 97 on Lever, i.e. more than half on just two of our three
  before counting Greenhouse. Multiple 2026 guides describe Ashby as the default
  for YC/AI startups (directional).
- **Enterprise:** Fortune 500 installs — Workday 39%+, iCIMS roughly 1 in 4, SAP
  SuccessFactors 13.2% (Jobscan 2025 via Pin). So ~75–80% of the F500 sits on
  systems with no public JSON board API, and each such employer posts thousands
  of roles — which is why the postings-weighted coverage is so much lower than
  company-weighted.
- **Mixed 12,000-company dataset (Jobscan):** Greenhouse 19.3%, Lever 16.6%,
  Workday 15.9%, iCIMS 15.3% — the all-sizes view.

## Do the public APIs expose everything?

Companies-using-ATS ≠ postings-visible. Verified per API:

- **Greenhouse Job Board API:** returns only live postings published to a live
  public board; confidential/internal reqs never appear. (The docs page went
  behind a sign-in during August 2026; claim verified against the July 26, 2026
  Wayback snapshot.)
- **Lever Postings API:** README verbatim — only published-state postings; "All
  other jobs are completely hidden from the jobs API."
- **Ashby Public Job Posting API:** published postings **including
  `isListed: false` direct-link-only jobs** — slightly more than the visible
  board; compensation is an opt-in query parameter.

Net: for a covered company, our fetch ≈ its public careers page (Ashby a bit
better). The invisible slice is confidential/exec reqs — small at startups,
larger at enterprises. The semantic doc should state this.

## The long tail and the JSON-LD fallback

- **Documented public feeds beyond the big three: only three more** — Workable,
  Recruitee (JSON), and Personio (XML feed) per Cavuno's 2026 survey. Everything
  else is undocumented-but-stable JSON endpoints or sitemaps:
  whenthisjobwasposted.com integrates 37+ platforms this way (SmartRecruiters,
  Rippling, Teamtailor, BambooHR, Workday via its cxs JSON endpoints,
  SuccessFactors via sitemap lastmod, iCIMS, Taleo, and more). The boundary is
  documentation and ToS, not technical reachability.
- **JSON-LD adoption is driven by Google for Jobs**, which requires per-URL
  `JobPosting` markup on crawlable pages (no data-submission API; the Indexing
  API only prompts crawls). Web Data Commons measurements: 7K → 50K+ pay-level
  domains publishing JobPosting 2017–2022 (721% growth), 5M entities in the 2022
  corpus; October 2023 corpus: 189.8M JobPosting quads across 61,024 hosts.
  Common Crawl samples popular pages shallowly, so these undercount adoption.
- **What JSON-LD reliably gives us:** on domains using JobPosting — datePosted
  95.8%, hiringOrganization 94.5%, jobLocation 92.4%, but baseSalary only 38.0%.
  So the fallback yields title/date/org/location at good fidelity and salary
  rarely — tag `fetch_method` provenance so JSON-LD-sourced rows never silently
  mix with API-sourced ones in analytics.
- **Rendering caveat:** ATS-hosted pages mostly emit static markup, but Workday
  career sites render client-side — reaching them means the undocumented cxs
  JSON route (a fourth integration style, ToS caution), not JSON-LD.

## Design consequences

1. The `add-company` probe ladder is validated and now has concrete rungs: big-3
   native → Workable/Recruitee JSON, Personio XML → JSON-LD on the careers URL →
   "not coverable, manual watch." Undocumented endpoints (SmartRecruiters,
   Workday cxs) are a deliberate later decision, not a default rung.
2. Postings-weighted humility: even at full ladder, ~15–30% of tech postings
   stay out of reach, and never-posted roles are invisible to any fetcher.
   Positioning and Stage 3 publications must scope claims to "public ATS
   postings of tracked companies."
3. Ashby's `isListed: false` behavior means we can legitimately see slightly
   more than a company's public board — worth surfacing as a small feature
   ("unlisted but public postings").

## Verifier corrections applied

- Greenhouse Job Board API docs URL now redirects to sign-in; cited via the
  2026-07-26 Wayback snapshot.
- Tracker counts drifted between research and verification (6sense Greenhouse
  22,407 → 23,455 within days) — all tracker numbers are date-stamped and
  treated as orders of magnitude.
- Pin report is dated July 20, 2026 (not June).
- Cavuno's "six public APIs" is five JSON + one XML (Personio).
- whenthisjobwasposted's "~54 integrations" is approximate; its page states 37
  platforms / 30+ direct API integrations.
- Google has no data-submission API, but its Indexing API can prompt crawls of
  job URLs — "no submission API" softened accordingly.
- The 800-startup census is one vendor's directional methodology (flagged on the
  citing page itself).

## References

### ATS market share

- [Fortune, July 2026](https://fortune.com/2026/07/27/greenhouse-ceo-daniel-chait-ai-doom-loop-job-seekers-spam-interview-applications-unemployment/)
  — ~175,000 live Greenhouse postings; the postings anchor.
- [Fortune, August 2026](https://fortune.com/2026/08/07/every-10-jobs-2539-applicants-greenhouse-ceo-daniel-chait-hiring-advice-openai/)
  — 2,539 applicants per 10 jobs; platform scale corroboration.
- [Greenhouse G2 release](https://www.prnewswire.com/news-releases/greenhouse-ranked-best-ats-in-the-g2-spring-2026-reports-302723947.html)
  — "more than 7,500 companies," March 2026.
- [Ashby Series D](https://www.ashbyhq.com/blog/culture/series-d) — July 2025;
  1,300 → 2,700+ customers, 135% YoY growth.
- [Bloomberry Ashby scan](https://bloomberry.com/data/ashby/) — 3,596 live Ashby
  boards, 2026-08-13; the live-board measurement pattern.
- [Pin ATS report](https://www.pin.com/blog/ats-market-share-report/) — July
  2026; startup segment shares and F500 enterprise shares.
- [6sense: Lever](https://6sense.com/tech/recruitment/lever-market-share) —
  tracker count (~8.6k, drifting); methodology bias example.
- [GH vs SR](https://6sense.com/tech/recruitment/greenhouse-vs-smartrecruiters)
  — 6sense tracker counts for Greenhouse and SmartRecruiters.
- [iCIMS](https://6sense.com/tech/recruitment/icims-recruit-market-share) —
  6sense tracker count (~12.8k).
- [TheirStack: Greenhouse](https://theirstack.com/en/technology/greenhouse) —
  cumulative ever-observed count (~31.5k); upper bound.
- [Enlyft: Workday](https://enlyft.com/tech/products/workday-recruiting) — 2,952
  detected; 63% over 1,000 employees; undercount example.
- [Greenhouse Job Board API](https://web.archive.org/web/20260726095716/https://developers.greenhouse.io/job-board.html)
  — archived docs: live published postings only.
- [Lever Postings API](https://github.com/lever/postings-api) — "All other jobs
  are completely hidden from the jobs API."
- [Ashby API](https://developers.ashbyhq.com/docs/public-job-posting-api) —
  `isListed: false` inclusion; compensation opt-in.
- [Growth](https://lists.w3.org/Archives/Public/public-vc-edu/2023Sep/0000.html)
  — JobPosting domains 9K → 50K+ (Sept 2023 note on the WDC series).

### Aggregators and JSON-LD

- [HiringCafe](https://hiringcafe.com/) — live counter 2026-08-14: 3,837,925
  jobs / 122,883 companies.
- [Scraper listing](https://apify.com/blackfalcondata/hiringcafe-scraper) —
  earlier HiringCafe snapshot: 2.8M+ listings, 46 ATS platforms.
- [whenthisjobwasposted](https://whenthisjobwasposted.com/about) — 37+ platforms
  with machine-readable data; the long-tail feasibility list.
- [Cavuno survey](https://cavuno.com/blog/ats-platforms-public-job-posting-apis)
  — the only documented public feeds: big-3 + Workable, Recruitee, Personio.
- [Google JobPosting docs](https://developers.google.com/search/docs/appearance/structured-data/job-posting)
  — the adoption driver; markup requirements.
- [WDC series paper](https://www.uni-mannheim.de/media/Einrichtungen/dws/Files_Research/Web-based_Systems/pub/Brinkmann-etal-TheWDCSchemaorgDataSetSeries-WWW2023.pdf)
  — Brinkmann et al., WWW '23; adoption growth and property density.
- [WDC Oct 2023 subsets](https://webdatacommons.org/structureddata/2023-12/stats/schema_org_subsets.html)
  — 189.8M JobPosting quads, 61,024 hosts.
- [Oct 2024](https://webdatacommons.org/structureddata/2024-12/stats/stats.html)
  — WDC report; crawl denominator: 37.4M domains, 44% with structured data.
- [WDC series index](https://webdatacommons.org/structureddata/schemaorg/) —
  provenance of the yearly extractions.
- [BLS JOLTS](https://www.bls.gov/jlt/) — 7.359M openings, June 2026 preliminary
  (verified via the public API).
- [LinkUp data](https://www.linkup.com/data/) — 10M daily active postings from
  86,000+ employer sites.
- [Plural comparison](https://pluralcareers.com/resources/articles/greenhouse-vs-ashby-2026-comparison)
  — Greenhouse 7,000+; Ashby Series D figures.
- [100Hires comparison](https://100hires.com/ashby-vs-lever.html) — Ashby 4,000+
  customers; the 800-startup census (directional).
