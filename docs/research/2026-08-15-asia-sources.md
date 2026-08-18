---
title: Asia expansion — fetchable sources for JP/TW/SG/HK
date: 2026-08-15
type: report
status: current
---

# Asia sources: Japan, Taiwan, Singapore, Hong Kong

Method: four research agents (Japan; Taiwan/HK; Singapore/SEA; cross-cutting CJK
technical), each followed by a citation verifier. Unlike earlier memos,
fetchability claims here were verified by **live endpoint probes** (curl, no
auth) on 2026-08-14/15 — job counts below are what the endpoints actually
returned. ~45 references.

Scope note: mainland China was removed from scope by decision on 2026-08-15. The
research (run before the decision) independently confirmed it would have been a
no-go: every major platform (BOSS直聘, 智联, 51job, 猎聘, 拉勾, 脉脉) is
login-walled, anti-bot fenced at the CDN edge, and robots-disallowed; Chinese
case law (the Weibo v. Maimai line, with follow-on judgments through 2024) is
platform-friendly with no hiQ-style safe harbor; and with no Google for Jobs in
the mainland there is no JSON-LD ecosystem. Chinese employers remain reachable
only through their global-ATS boards for overseas/HK roles.

> [!TLDR] Asia works through the same three lanes we already planned
>
> Lane 1 — existing connectors: PayPay, Appier, OKX, Agoda, Stripe-APAC are live
> on Greenhouse today; Crypto.com, Animoca, Ninja Van, Nium on Lever. Lane 2 —
> two new documented public APIs: Workable (unlocks Mercari, SmartNews) and
> SmartRecruiters (unlocks Grab, 349 postings), plus Singapore's government
> MyCareersFuture API — the single highest-coverage source in Asia. Lane 3 — the
> planned JSON-LD fetcher is unusually productive in Japan (HERP, HRMOS, Green,
> Wantedly all emit JobPosting markup) and covers Taiwan's Cake. The real new
> work is not sources — it's CJK plumbing: SQLite's default tokenizer silently
> breaks CJK search, no taxonomy has Japanese/Chinese labels, and salary parsing
> needs per-market rules (万円, ×13薪, monthly-by-default).

## Verified-fetchable source map

### Japan

- **Already ours:** PayPay on Greenhouse (85 jobs, live-verified).
- **Workable connector unlocks:** Mercari (138 jobs) and SmartNews (20) via
  `apply.workable.com/api/v3/accounts/{token}/jobs` — public, unauthenticated.
- **Domestic ATSes via JSON-LD:** HERP boards at
  `herp.careers/careers/companies/{token}` and HRMOS boards at
  `hrmos.co/pages/{token}/jobs` both emit full JobPosting JSON-LD (with salary
  as MonetaryAmount) on job detail pages, with permissive robots. engage
  and ジョブカン are likely-JSON-LD (Google-indexed) but unverified per-company.
- **Platforms:** Green and Wantedly emit JobPosting JSON-LD on public pages
  (Wantedly needs a ToS/norms check; its postings are often salary-less
  culture-fit ads). Forkwell has a public sitemap but no JSON-LD (HTML parsing —
  skip for now). Findy, LAPRAS, BizReach: login-fenced, same class as LinkedIn —
  never.
- **Self-hosted majors:** Rakuten (Phenom, geo-fenced from US IPs), LY Corp,
  Cybozu, Woven, Preferred Networks — no ATS board found; JSON-LD/per-site seeds
  are the only route. Sony works via Workday's undocumented CXS JSON endpoint
  (live-verified, but gray-zone — no contract).
- **Government:** Hello Work has an API but restricted to municipalities and
  licensed employment agencies — not usable. Japan's legal posture is friendly
  (no CFAA analog for public pages; Copyright Act Art. 30-4 permits machine
  information-analysis); keep polite rate limits (the Librahack incident is the
  cautionary tale).
- **Market structure:** the new-grad (新卒) Rikunabi/Mynavi cycle is
  account-gated and not useful; job-hunter's value is the mid-career (中途)
  market, where employer-direct sources beat the paid-listing boards.

### Taiwan

- **Cake (cake.me)** — strongest new source: fully permissive robots, jobs and
  companies sitemaps, valid JobPosting JSON-LD on job pages. Fits the planned
  JSON-LD lane exactly; tech-focused and English-friendly.
- **Yourator** — undocumented but open JSON API
  (`yourator.co/api/v4/jobs?page=N`, live-verified, no auth) with permissive
  robots. Version-pin the parser; polite rate limits.
- **Appier on Greenhouse** (88 jobs, live-verified). Gogoro, KKday, KKBOX,
  17LIVE not found on big-3 under obvious slugs; TSMC is on Eightfold (endpoint
  403s non-browser clients) and MediaTek runs a custom site — big TW hardware is
  effectively enterprise-fenced.
- **104人力銀行** — its famous undocumented JSON endpoints are now behind
  Cloudflare managed challenges (403 even for robots.txt from datacenter IPs).
  Default off; at most a user-initiated, browser-fidelity, opt-in fetch. 1111:
  reachable but no JSON-LD found, low value.

### Singapore / SEA

- **MyCareersFuture (government job bank)** — the standout:
  `api.mycareersfuture.gov.sg/v2/jobs` and `/v2/search` return rich JSON (salary
  ranges, skills, experience) with no key and no bot challenge (live-verified).
  Employment Pass regulation forces essentially all formal SG postings onto it.
  Undocumented, so: courtesy-API discipline — conservative rate, honest
  User-Agent, raw snapshots, schema-drift alarm. data.gov.sg itself has
  aggregate labour statistics only.
- **SmartRecruiters connector unlocks Grab** (349 postings, live-verified) via a
  _documented_ public Posting API — same class as Greenhouse/Lever; worth adding
  as a first-class fourth ATS.
- **On existing connectors:** Agoda (286 jobs) and Stripe (578 jobs; its
  `/offices` endpoint lists SG/HK/Tokyo/APAC, so office-filtering works) on
  Greenhouse; MoneyHero, Wise partial on Greenhouse; Ninja Van and Nium on
  Lever.
- **Fenced (skip):** JobStreet/JobsDB (SEEK — robots disallows every job page,
  `/graphql`, and AI crawlers; 403s curl), NodeFlair, Glints, Tech in Asia.
  JSON-LD on a robots-fenced site is not a loophole.
- **No legitimate route today:** Sea/Shopee/Garena, GoTo/Gojek, ByteDance/
  TikTok SG, Carousell — custom career sites with unstable internal APIs.
  Partially covered indirectly: EP-compliance cross-posting puts many of their
  SG roles on MyCareersFuture. Re-check quarterly.

### Hong Kong

- **On existing connectors:** OKX on Greenhouse (327 jobs), Crypto.com on Lever
  (26), Animoca Brands on Lever (8) — all live-verified. Banks and conglomerates
  are on Workday/Avature/SuccessFactors (no public API); LinkedIn dominance
  otherwise. JobsDB is fenced (same SEEK policy). HK coverage will come from
  panel curation, not a new source.

## Cross-cutting technical requirements (the real new work)

1. **FTS5 breaks silently on CJK.** Empirically confirmed (SQLite 3.43.1): with
   default unicode61, `機械学習` does not match a row containing
   `機械学習エンジニア` — the whole CJK run is one token. Adopt: per-language
   routing at ingest; the built-in `trigram` tokenizer (SQLite ≥ 3.34) for CJK
   content — verified working, ~3x index size, with a documented limitation
   (queries under 3 chars match nothing; mitigate with indexed `LIKE`) — and a
   pluggable tokenizer knob for power users (lindera for Japanese dictionaries,
   wangfenjin/simple for Chinese).
2. **No taxonomy has CJK labels.** ESCO ships 28 languages — 24 EU official plus
   Icelandic, Norwegian, Ukrainian, Arabic; no Japanese or Chinese (confirmed on
   the official page). Lightcast Open Skills is English-only in practice. Plan:
   keep the English canonical spine; attach Japanese labels via the Japanese
   O-NET ("job tag") downloadable CSVs (O*NET-modeled, so an
   ESCO↔O*NET↔Japanese-O-NET crosswalk is feasible; ship a downloader script,
   not the data — JILPT terms require citation and usage notice). Bridge CJK
   text → canonical skills with multilingual embeddings, not string lookup.
   Softener: tech postings write hard skills in Latin script ("Python",
   "Kubernetes") even in Japanese/Chinese text.
3. **Embeddings:** multilingual-e5-small (MIT, ~118M params, 384-dim,
   JA/ZH-capable) as the CPU-cheap default; Qwen3-Embedding-0.6B (Apache-2.0,
   MTEB-multilingual 64.33 vs BGE-M3's 59.56) as the quality tier. Vectors stay
   in sqlite-vec. Cross-lingual match (English query ↔ Japanese posting) is
   exactly what these models train for — it substitutes for the missing CJK
   taxonomies.
4. **Normalization at ingest:** NFKC (full-width digits/kana: `６００万円` →
   `600万円`) and OpenCC traditional↔simplified folding so Taiwan and
   mainland-script text cross-match. Raw snapshots stay untouched.
5. **Salary parsing needs ~200 lines of per-market rules** (no library does
   this; price-parser confirmed not to handle 万 multipliers or k-notation):
   Japan 年収 N万円 = annual ×10,000 JPY; Taiwan/SG/HK are monthly-by-default (a
   US annual-by-default assumption silently understates by 12x); bonus
   structures like ×13–16薪 annualize by month count. Schema: keep raw string
   plus `{min, max, currency, period, months_per_year}`; annualize only at query
   time. JSON-LD's `baseSalary.unitText` (MONTH/YEAR) is trustworthy when
   present.
6. **JSON-LD availability:** Google's job-search experience covers Japan,
   Singapore, Hong Kong (and Taiwan via GFJ) — so markup is generally present on
   Google-indexed boards there. Caveat: some Japanese career sites render job
   pages client-side even when JSON-LD exists on detail pages — the fetcher
   needs a JS-rendering fallback path or per-company seeds. No per-market
   adoption measurement exists (WDC has no language breakdown).

## Schema deltas to adopt now

- `language` (BCP-47) and `country` columns on postings; `currency`, `period`,
  `months_per_year` on parsed salary; `fetch_method` already planned — add
  values for `workable`, `smartrecruiters`, `mcf`, `jsonld`, `yourator`.
- FTS routing flag per posting (which index it went to).
- Panel entries get a `coverage` result from the add-company probe, whose ladder
  gains rungs: big-3 → Workable/SmartRecruiters/MCF → JSON-LD → Yourator-class
  undocumented (opt-in) → not coverable.

## Connector priority (effort vs coverage)

1. **Workable** — documented public API; Mercari + SmartNews day one.
2. **MyCareersFuture** — one endpoint, near-total SG formal-market coverage.
3. **SmartRecruiters** — documented public API; Grab + global enterprises.
4. **Generic JSON-LD fetcher** — one component covers HERP, HRMOS, Green,
   Wantedly, Cake, engage, and self-hosted JP pages; the highest-payoff single
   build for Asia and it was already on the roadmap.
5. **Yourator** — small, clean, undocumented; cheap to add behind a flag.
6. Deferred/gray: Workday CXS (Sony et al.), Eightfold (TSMC), 104 — all fenced
   or contract-less; revisit only with explicit opt-in design.

## Verifier corrections applied

- The Green example URL redirects to its canonical `/company/{id}/job/{id}` form
  (JSON-LD confirmed there).
- Greenhouse's Job Board API docs URL now redirects to a sign-in page; claims
  rest on the live API probes (and the Wayback snapshot noted in the 2026-08-14
  coverage memo).
- The NeuData Weibo-v-Maimai analysis is from 2021 (not 2019); the ciplawyer
  case note is a 2024 follow-on judgment in the Weibo line, not the original
  case. (Both now only inform the China-exclusion note.)

## References

### Japan

- [PayPay board](https://boards-api.greenhouse.io/v1/boards/paypay/jobs) —
  Greenhouse; 85 jobs, live-verified 2026-08-15.
- [Mercari](https://apply.workable.com/api/v3/accounts/mercari/jobs) — public
  unauthenticated Workable JSON; 138 jobs.
- [SmartNews](https://apply.workable.com/api/v3/accounts/smartnews/jobs) —
  second Japanese Workable tenant.
- [HERP board index](https://herp.careers/careers) — cross-company entry point
  for hosted boards.
- [HERP job](https://herp.careers/careers/companies/notainc/jobs/_zcN5AqiK3i5) —
  example page; full JobPosting JSON-LD incl. MonetaryAmount.
- [HRMOS ZOZO board](https://hrmos.co/pages/zozo/jobs) — public board pattern;
  JSON-LD on detail pages.
- [Wantedly example](https://www.wantedly.com/projects/2436930) — JobPosting
  markup on public project pages; ToS review advised.
- [Green example](https://www.green-japan.com/job/326169) — JobPosting JSON-LD
  incl. salary (redirects to canonical company URL).
- [Forkwell sitemap](https://s3-ap-northeast-1.amazonaws.com/forkwell/sitemaps/production/jobs/sitemap.xml.gz)
  — public discovery, but no JSON-LD on pages.
- [Sony Workday CXS](https://sonyglobal.wd1.myworkdayjobs.com/wday/cxs/sonyglobal/SonyGlobalCareers/jobs)
  — undocumented POST endpoint, works unauthenticated; gray-zone.
- [Hello Work API](https://www.hellowork.mhlw.go.jp/provide/provide_top.html) —
  government provision service restricted to licensed agencies.
- [HERP robots](https://herp.careers/robots.txt) — permissive;
  crawl-friendliness evidence.

### Taiwan and Hong Kong

- [Cake sitemap](https://www.cake.me/sitemap.xml) — permissive robots, jobs
  sitemaps, JobPosting JSON-LD; strongest new Taiwan source.
- [Yourator API](https://www.yourator.co/api/v4/jobs?page=1) — open undocumented
  JSON, live-verified.
- [Appier board](https://boards-api.greenhouse.io/v1/boards/appier/jobs) —
  Greenhouse; 88 jobs.
- [OKX board](https://boards-api.greenhouse.io/v1/boards/okx/jobs) — Greenhouse;
  327 jobs.
- [Crypto.com on Lever](https://jobs.lever.co/crypto) — 26 postings via the
  Lever JSON API (Animoca similar).
- [JobsDB robots](https://hk.jobsdb.com/robots.txt) — disallows every job page,
  `/graphql`, search APIs; the fence evidence.
- [SEEK developer portal](https://developer.seek.com/) — partner-gated API, not
  a public feed.
- [BOSS Zhipin robots](https://www.zhipin.com/robots.txt) — search and token
  endpoints disallowed (China-exclusion record).
- [Weibo v. Maimai analysis](https://www.neudata.co/alternative-data-news/chinese-weibo-vs-maimai-case-shows-limitations-of-web-scraping)
  — NeuData, 2021; the platform-friendly doctrine (exclusion record).
- [2024 follow-on case note](https://www.ciplawyer.com/articles/153857.html) —
  Chinese unfair-competition scraping judgment (exclusion record).

### Singapore / SEA

- [MCF jobs API](https://api.mycareersfuture.gov.sg/v2/jobs?limit=2&page=0) —
  live-verified open JSON; the de-facto SG feed (undocumented).
- [MyCareersFuture](https://www.mycareersfuture.gov.sg/) — government job bank;
  EP-regulated roles must be advertised here.
- [data.gov.sg datasets](https://api-production.data.gov.sg/v2/public/api/datasets?query=vacancy)
  — aggregate statistics only, no posting-level data.
- [JobStreet robots](https://sg.jobstreet.com/robots.txt) — SEEK fencing; blocks
  AI crawlers from job paths.
- [Posting API](https://developers.smartrecruiters.com/reference/postingsget-1)
  — SmartRecruiters; documented public API; the fourth-ATS candidate.
- [Grab postings](https://api.smartrecruiters.com/v1/companies/Grab/postings) —
  349 live postings, verified.
- [Agoda board](https://boards-api.greenhouse.io/v1/boards/agoda/jobs) — 286
  jobs on Greenhouse.
- [Stripe offices](https://boards-api.greenhouse.io/v1/boards/stripe/offices) —
  SG/HK/Tokyo/APAC office entities for location filtering.
- [Ninja Van on Lever](https://api.lever.co/v0/postings/ninjavan?mode=json) —
  verified; Nium similar.
- [TikTok careers robots](https://lifeattiktok.com/robots.txt) — search-bot
  allowlist; internal APIs unstable.
- [Shopee careers](https://careers.shopee.sg/) — custom SPA, no public API.
- [Glints robots](https://glints.com/robots.txt) — fenced explore/search.

### Cross-cutting technical

- [What is ESCO](https://esco.ec.europa.eu/en/about-esco/what-esco) — 28
  languages, none CJK.
- [Lightcast Open Skills](https://lightcast.io/open-skills) — English-only in
  practice.
- [MHLW codes](https://www.hellowork.mhlw.go.jp/info/mhlw_job_dictionary.html) —
  Japan's official occupation classification, 2022 revision.
- [Japanese O-NET downloads](https://shigoto.mhlw.go.jp/User/download) —
  CSV/Excel occupational database (v7.x, 2026); crosswalk enabler.
- [China occupation dictionary](https://zh.wikipedia.org/wiki/%E4%B8%AD%E5%8D%8E%E4%BA%BA%E6%B0%91%E5%85%B1%E5%92%8C%E5%9B%BD%E8%81%8C%E4%B8%9A%E5%88%86%E7%B1%BB%E5%A4%A7%E5%85%B8)
  — 2022 edition structure; no official machine-readable release.
- [SQLite FTS5 doc](https://sqlite.org/fts5.html) — unicode61 vs trigram
  semantics; under-3-char limitation; indexed LIKE.
- [SQLite 3.34 release](https://sqlite.org/releaselog/3_34_0.html) — dates
  trigram support (2020-12).
- [wangfenjin/simple](https://github.com/wangfenjin/simple) — Chinese FTS5
  tokenizer (cppjieba + pinyin).
- [lindera-sqlite](https://github.com/lindera/lindera-sqlite) — dictionary
  tokenizers for JA/KO/ZH.
- [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)
  — MIT; default CJK-capable embedder.
- [BGE-M3](https://huggingface.co/BAAI/bge-m3) — MIT; heavier multi-mode
  alternative.
- [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) —
  Apache-2.0; best small multilingual per MTEB.
- [Google JobPosting docs](https://developers.google.com/search/docs/appearance/structured-data/job-posting)
  — regional availability (JP/SG/HK listed; mainland absent); baseSalary
  unitText.
- [price-parser](https://github.com/scrapinghub/price-parser) — confirmed
  no万/k-notation support; motivates custom salary rules.
- [WDC table corpus](https://webdatacommons.org/structureddata/schemaorgtables/)
  — only large JobPosting corpus; no language breakdown.
