# Research memo: from job postings to labor-market insight

*Sonnet research agent, 2026-08-08. Sourced via web search.*

## 1. The established players

- **Indeed Hiring Lab**: Job Postings Index (Feb-2020=100, 7-day trailing avg,
  Bundesbank daily seasonal adjustment, open data on GitHub), Wage Tracker (median
  posted wage by title-region cell, YoY of cells, median of that — Atlanta-Fed-style
  composition control), remote tracker (~8% fully remote 2025).
- **Lightcast**: 220k+ sources, ~36h pipeline; self-discloses coverage: ~60% of online
  vacancy stock, ~40% of flow. Open Skills taxonomy (34k skills, ~13/posting).
- **LinkUp**: 80k+ employer career sites crawled directly; S&P 500 LinkUp Jobs Index
  (weekly, fixed company universe) — the fixed-panel index model.
- **Revelio Labs**: postings + profile data; posting-to-hire matching.
- **hiring.cafe**: peer/existence proof — small team aggregating ATS APIs (Greenhouse/
  Lever/Ashby/Workday +) with LLM summaries; consumer product, no research arm.
- **levels.fyi**: crowdsourced realized offers (245k+ points) — methodological foil:
  posted ranges vs realized comp answer different questions.

## 2. Government benchmarks

- **JOLTS**: openings/hires/quits, 5–7 week lag; response rate collapsed 58% (2019) →
  ~35% (2025) — read as trend, not level. Postings indices lead JOLTS by weeks
  (practitioner claim, Indeed).
- **OEWS**: paid base wages, ~3-year smoothed panels — no real-time signal; postings
  measure *asking* wages, OEWS measures *paid*.

## 3. Standard metrics

Postings index vs fixed baseline; posted-wage growth via title-matched cells (needs
salary coverage — a pay-transparency-era artifact); skill-demand trends (Lightcast AI
postings +73% then +109% YoY); remote share; posting duration as time-to-fill proxy
(evergreen tail: 25% >90d, 10% >180d confounds it); tightness V/U (Domash NBER 29739
proposes vacancy-to-effective-searchers, correlates better with wages).

## 4. Known biases

- Representativeness: online ads ≈ representative except industry (Marinescu/Rathelot);
  large formal employers over-represented.
- Posting ≠ vacancy ≠ hire: **Ashby's own 22k-posting study: 18% closed without
  hire** (5.5% paused; explicitly a lower bound). Ghost estimates 18–27% elsewhere.
- Duplication: Lightcast removes up to 80% via 60-day windows.
- **ATS-public-API subset bias (ours)**: VC-backed tech, formal recruiting, US hubs,
  structured roles. Excludes Workday/iCIMS enterprises, government/health/retail.
  **Nobody has quantified this specific bias — open research gap.**
- Standard caveating: validate aggregates vs BLS; report growth rates not levels;
  state the sampling frame explicitly.

## 5. Key academic work

- Remote: Hansen/Bloom/Davis et al. NBER 31007 + WFH Map (LLM classifier on 500M
  postings; US remote share ~3× 2019–25); cross-validated against SWAA surveys.
- Pay transparency: Arnold, Quach & Taska NBER 34480 (mandates +30pp disclosure,
  +1.3–3.6% wages); Batra/Michaud/Mongey NBER 31984 (pre-law wage coverage ~5% —
  why transparency-era data is the only credible posted-wage window).
- Concentration/monopsony: Azar/Marinescu/Steinbaum/Taska NBER 24395 (HHI by
  commuting-zone × occupation). Skills: Deming & Kahn 2018; Hershbein & Kahn AER 2018.
- AI: Acemoglu/Autor/Hazell/Restrepo NBER 28257; Alekseeva et al. 2021 (AI demand
  ~10× 2010–19, 11% within-firm premium); Brynjolfsson/Chandar/Chen 2025 (ADP payroll,
  not postings — 13–16% early-career decline in AI-exposed roles).

## 6. Opportunity assessment for our corpus

**Reality check**: 2–4 orders of magnitude smaller than Lightcast/Indeed. Never claim
"the labor market." Small cells (role × level × geo × quarter) will have single-digit N.

**Precedent for credibility at small scale**: Ashby's 22k-posting ghost study (same
order of magnitude as us — cited by mainstream press because transparently scoped),
layoffs.fyi / TrueUp (narrow scope + public data), Guy Berger's solo Substack,
Burning Glass Institute (narrow timely questions, e.g. No Country for Young Grads:
entry-level share of AI-exposed roles 43% → 28%, 2018–24).

**Our structural advantages** (what majors don't publish):
1. **Full posting lifecycle per company** — freeze/thaw detection, repost cadence,
   evergreen identification at company level. Needs temporal completeness, not scale.
   The single most defensible "only we can do this" angle.
2. **Coherent population**: "VC-backed ATS-modernized tech" is describable; scope
   claims to it are defensible.
3. **Pay-transparency-era structured salary**: our population concentrates in
   early-mandate states (CA/NY/WA/CO) — highest-compliance segment. Real advantage.

**Ranked credible analyses**: (1) narrow posted-wage tracker for VC-backed tech
(growth rates, title-matched cells, direction-validated vs Indeed/JOLTS); (2) ongoing
freeze/thaw + evergreen detection; (3) tech-specific pay-transparency DiD replication;
(4) AI-title diffusion curves; (5) remote divergence within tech; (6) within-corpus
tightness proxy.

**Where we'd fail scrutiny**: point estimates from small cells (enforce min-N +
buckets); unstable company panel (need a **versioned, dated panel** with disclosed
changes); **ATS choice as confound** (Ashby exposes structured salary, Greenhouse
buries it — salary samples over-represent Ashby customers; model or restrict);
duplicates/ghosts swing small denominators; causal language without DiD design;
multiple comparisons (pre-specify a few repeated metrics — a tracker, not one-off
charts); no clean official validation series exists for our population (standing
limitation, state it).

**Bottom line**: the lane is Berger/BGI/Ashby-blog — few, narrow, transparently-scoped,
repeatable metrics with a versioned panel and published methodology; growth rates not
levels; lifecycle analytics and pay-transparency analysis are the differentiated assets.
