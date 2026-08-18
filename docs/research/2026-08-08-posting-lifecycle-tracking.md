# Research memo: tracking job postings longitudinally

*Sonnet research agent, 2026-08-08. Sourced via web search.*

## 1. Posting lifecycle methodology

- **Lightcast**: closure detected by re-fetch + "no longer active" pattern matching, plus
  a hard 120-day auto-expire. Explicitly does NOT classify *why* a posting closed —
  active/expired only. First/last-seen per source; multi-source postings expire only
  when all sources expire.
- **LinkUp**: crawls 86k+ employer career sites directly — "live" means live on the
  company's own site. Stronger than aggregator delisting, still not proof of a hire.
- **Indeed Hiring Lab Job Postings Index**: macro daily volume vs Feb-2020 baseline,
  seasonally adjusted (Bundesbank method), validated against BLS/Eurostat. (GitHub:
  hiring-lab/job_postings_tracker)
- **Duration estimates vary by source**: JOLTS microdata mean 14–25 days (Davis,
  Faberman & Haltiwanger 2013, QJE — and each "opening" yields 1.0–1.8 hires); UK
  online-ads near-universe 17–18 days (Bassier, Manning & Petrongolo 2025, LSE);
  LinkUp-based 36.5 mean / 23 median with >180d trimmed. ~25% of postings stay up
  >90 days; ~10% >180 days (evergreens defeat fixed cutoffs).
- **Dalton, Kahn & Mueller (NBER WP 34012, 2025)**: online postings covered 22% of
  JOLTS openings in 2007 → 49% in 2020 (coverage is partial and time-varying); one
  posting ≈ ~2.5 simultaneous openings on average.
- **"Closed" is ambiguous**: filled externally / filled internally / withdrawn /
  budget-cut / expired. Best validated "filled" proxy: Revelio Labs posting-to-hire
  matching — hires-per-posting fell 0.75 (2018) → <0.5 (2023). Needs external hire
  data we won't have; note as limitation.

## 2. Deduplication

- **Lightcast**: normalized title+company+location key in a 60-day window; removes up
  to 80% of raw records. Known blind spot: reposts on longer cycles counted as new.
- **Textkernel**: avg ad reposted 2–5×; 50–80% duplicates in raw feeds. Technique:
  shingling → MinHash → inverted index; boilerplate removal; classifier over text +
  metadata. **True duplicates share as little as 37% raw text similarity** — naive
  thresholds fail; multi-signal is mandatory.
- Academic: embeddings + domain knowledge hybrids (arXiv:2406.06257, LKE 2024);
  multilingual embedding dedup (arXiv:2406.13695).
- **Converged pattern**: normalized (company, title, location[, level]) blocking key →
  fuzzy/embedding confirmation → bounded time window.

## 3. Repost/refresh gaming

- Greenhouse ships a first-class "refresh free job posts" feature — recency-gaming is
  a native ATS workflow. LinkedIn/Indeed ranking rewards freshness.
- Share of postings active past 30 days halved 2022 (68%) → 2025 (~33%) (Clarify
  Capital) — likely faster close/repost cycling, not faster hiring.
- Appcast 2024: ~1 in 4 recruiters explicitly instructed to keep postings live "for
  visibility."
- Correction pattern: never trust platform posting_id as identity; track normalized-
  identity clusters with first_seen/last_seen/reappeared_at; closed-then-reappeared
  within N days = repost signal.

## 4. Ghost jobs

- Prevalence estimates converge ~15–35%: MyPerfectResume (81% of recruiters say their
  employer posts them; ~1 in 3 listings), ResumeBuilder (43% post to project growth),
  Clarify Capital 2025 (~1 in 3 employers admit no intent to hire), **Greenhouse
  telemetry: 18–22% of postings on Greenhouse classified ghost in a given quarter**;
  WSJ: ~70% of Greenhouse companies posted ≥1 ghost job in Q2 2024.
- Academic: Hunter Ng (arXiv:2410.21771) — LLM+BERT on Glassdoor, up to 21%, links to
  Beveridge-curve disconnect. Business Economics 2025 — adjusting openings series for
  ghost trends reduces the vacancies-quits disconnect. Grimm, Columbia Law Review 2025 —
  ghost jobs as FTC unfair/deceptive practice.
- **Detection signals** (consistent, not individually validated): age >60–90d without
  content change; evergreen language; repost cycles without edits; missing salary where
  disclosure is normal/required; no response post-application. Evergreen ≠ ghost —
  surface signals identical; only outcomes separate them.

## 5. Change tracking (the gap = our opportunity)

- Most research treats postings as single snapshots. **No major dataset publishes
  field-level edit history.** A local tool diffing every field daily captures a
  genuinely under-studied signal.
- Best-studied edits: salary fields, via pay-transparency rollouts. Arnold, Quach &
  Taska (NBER WP 34480, 2025): mandates raised disclosure ~30pp; posted salaries +3.6%
  for continuing posters; wage effect 1.3–3.6%; no effect on volume/requirements.
  Voluntary disclosure also rose ~30pp 2019–23 (Minneapolis Fed).
- "Requisition drift" is recognized in recruiting ops (scope/level/pay shifting
  mid-cycle without posting edits) but unmeasured in research.

## 6. Signals for job seekers

- **Ashby's own 13M-application study (93k postings, 2021–23): first week of a posting
  sees ~2× the application rate of any later week**; weekly apps-per-job tripled over
  the period; first 4 weeks predict future application rate. Best available evidence
  that applicant volume is front-loaded.
- Marinescu & Wolthoff (J. Labor Econ 2020): higher posted wages → more applicants,
  shorter duration — content, not just age, drives competition.
- "Apply within 48h" / "3× response" claims: vendor folklore, no methodology. No clean
  causal apply-timing → callback evidence exists publicly.

## 7. Schema implications (adopted into our data layer)

Entities: raw_snapshot (verbatim payload, never discard), posting_identity (normalized
composite key, separate from platform id), duplicate_cluster (+match_method,
confidence), company (canonical; metrics are self-referential to company baselines).

Events: first_seen / last_seen; closed with **closure_signal** (delisted vs explicit
status vs 404) + confidence; **reappeared**; **field_changed** (per field, old/new,
diff magnitude to filter formatting churn); **content_hash_changed vs unchanged on
refresh** (distinguishes recency-gaming from real edits).

Derived metrics the events must support: posting_duration (vs company's own historical
median, not fixed cutoffs), repost_count/interval, edit_count/magnitude,
days_to_first_salary_disclosure, **ghost_score** (composite: age-without-edits, repost
cycles, salary absence, evergreen-text reuse), hires_proxy (same-company near-dup
reopening within N days = probably-not-filled).

**Core lesson**: every third-party provider baked irreversible judgment calls into
collection (60-day windows, snapshot-only records). Keep raw snapshots + rich events;
defer all classification to query time so heuristics can improve retroactively.
