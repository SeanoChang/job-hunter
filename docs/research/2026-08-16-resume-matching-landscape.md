---
title: Resume-matching prior art — documented difficulties and fixes
date: 2026-08-16
type: report
status: current
---

# AI resume matching: how others struggled, what they did

Method: 6 agents — 3 Sonnet finders (effort high; products / research literature
/ practitioner writeups), each followed by an Opus adversarial citation verifier
(a tier above the finder, per the session verification standard). 44 references;
every URL fetched or publisher-confirmed. The research-literature lens verified
fully clean; corrections to the other two were metadata-level
(titles/years/framing) and are applied below.

> [!TLDR] The graveyard has a consistent layout
>
> Every documented failure falls into six pits: judge variance (the same resume
> scored 66–99 across 100 runs at temperature 0.1), silent LLM scaffold failures
> (a null-handling bug killed a feature for months, unnoticed), parsing as the
> real cost center (a 28k-star matcher's issue tracker is mostly extraction
> bugs), gameable single scores (Jobscan's own help copy admits the tension),
> measured demographic bias (85.1% white-name favoritism in embedding retrieval;
> Amazon's model un-fixable), and an audit regime that structurally failed
> (LL144 "null compliance"). Our claims-grammar design already guards five of
> the six pits; the new adoption it forces is treating every judge verdict as a
> distribution — repeated sampling with confidence backing, calibrated against a
> small human-labeled sample.

## The difficulty catalog

### 1. LLM-judge verdicts are unstable — the sharpest finding

An independent engineer reproduced HackerRank's open-sourced LLM resume scorer
and ran the _same resume_ repeatedly: 90, then 74, then 88; at temperature 0.1
across 100 runs, scores ranged **66–99** — with a hiring cutoff of 85, the
identical resume failed 65% of the time on luck. Root cause was not temperature:
unanchored rubric categories ("architectural complexity") genuinely vary in
model judgment, and even temp=0 is non-deterministic on real GPU serving. The
1,032-point HN thread confirmed practitioners see the same at scale. **The fix
with evidence:** Indeed treats output variance as a statistics problem — run
each input k=3–5 times, cluster-bootstrap the results into confidence intervals;
they show a 3.9-point model gap dissolving into noise while a 42.3-point gap
holds.

### 2. Raw LLM classification rates are miscalibrated

Indeed's LLM classifier flagged 56.4% of job recommendations as having
work-experience mismatch problems; validating ~100 records against humans showed
sensitivity 73.5% / specificity 77.8% — errors in _both directions_ — and the
Rogan-Gladen correction moved the true estimate to 66.6%, a 10-point shift that
reversed a resourcing decision. Prompt tuning was not the fix; a human-labeled
calibration sample was.

### 3. Parsing is where the effort actually goes

Resume-Matcher (28k stars) is the honest OSS record: its multi-year issue
history is dominated by extraction, not matching — CJK text garbled from
PDF/DOCX (issues #484/#777), a remediation PR documenting Hangul at 0.7% font
coverage and Simplified-Chinese glyphs hijacking Japanese kana. The commercial
vendors confirm the plateau: Textkernel, decades in, still ships targeted fixes
for column-layout resumes; Affinda argues 95% field accuracy is explicitly not
good enough for automation. Residual extraction error is irreducible — the
design choice is whether it's visible or silently absorbed into scores.

### 4. Silent scaffold failure is the default failure mode

Two Resume-Matcher bugs are the canonical pair. Issue #791: a scanned PDF
extracted to empty text, the pipeline didn't stop, and the LLM invented a
plausible "John Doe" resume that was **stored as the user's data**. PR #825: the
prompt said send `"original": null`, the live model sent a list anyway, a type
check silently dropped every such change — an entire feature dead for months,
logged only as an unwatched warning. Both fixes were the same shape:
reject-on-empty, accept-and-validate-narrowly, fail loud.

### 5. Single scores breed gaming and distrust

Jobscan's own help copy targets a ~75% match rate while warning that going
higher risks "overstuffing your resume with keywords" — the vendor documenting
that its metric rewards what its advice forbids. Resume-Matcher issue #81 is the
user side: "where is the score actually computed?" Searchlight (YC W19) is the
strongest verdict: they abandoned resume-only matching entirely because resumes
structurally reward self-promotion over substance. And keyword-stuffing (white
text, 1pt fonts) is a documented arms race.

### 6. Embedding similarity has a measured ceiling

The ConFit arc (one team, three papers, 2024–2026) is the field admitting it: v1
fought label sparsity with augmentation and contrastive learning; v2 added
hypothetical resumes and hard negatives; v3 concluded embedding-only retrieval
"fundamentally cannot give reasoning or controllability" and pivoted to LLM
re-ranking — which itself needed multi-pass ranking and noise filtering before
beating frontier-model baselines. LinkedIn's recruiter-search team reported
embedding similarity gave statistically insignificant ranking gains, and
tree-based rankers systematically starved sparse-but-decisive features like
skill IDs; their real win came from changing the objective (mutual interest)
rather than the representation.

### 7. Bias is measured, compounding, and partly human-side

Wilson & Caliskan's audit of production embedding models (500+ real resumes ×
500 JDs, 9 occupations): white-associated names favored in **85.1%** of
comparisons, female-associated in 11.1%, Black-male candidates disadvantaged in
up to 100% for some occupations — with resume _length_ and name
_corpus-frequency_ as previously uncontrolled confounds. Their FAccT 2026
follow-up shows the second layer: humans lean on AI recommendations when present
(spending up to 55.6% longer only when no recommendation exists), so model-layer
mitigation doesn't fix the collaboration layer. Counterfactual explanations
didn't prevent bias adoption in-session but reversed it afterward — and
self-reported trust was uncorrelated with protection. ICML 2025 adds that LLM
errors are _correlated across models_ sharing lineage, worsening with scale —
averaging judges from one provider is not an ensemble. The history: Amazon's
model (downgrading "women's chess club captain") was killed as un-fixable;
iTutorGroup paid $365,000 to the EEOC; HireVue dropped facial analysis under
FTC-complaint pressure; _Mobley v. Workday_ established that the _vendor_ of
screening algorithms can face disparate-impact claims, with a nationwide
age-collective conditionally certified in 2025.

### 8. The audit regime failed structurally — design for inspectability

NYC Local Law 144 is the cautionary tale: undefined terms produced four
incompatible auditor roles; a 391-employer field audit found **18** posted audit
reports ("null compliance" — a missing report proves nothing); the law's own
metrics are mathematically too weak to catch distributional bias; and
supply-chain structure means no single party has both responsibility and
visibility. The EU AI Act instead classifies employment screening as high-risk
by default (Annex III). Note our different posture: job-hunter is a
_candidate-side_ tool assessing jobs for one user — not an employer screening
candidates — but the lesson transfers: an inspectable per-requirement evidence
trail beats after-the-fact audit claims.

### 9. Benchmarks mislead

The field lacked real benchmarks (RJDB synthesized labels from GPT-4 over a
skill graph); when a real one arrived (PJB, ~200k resumes), it showed aggregate
scores actively mislead — industry domain swamps model/module choice, and one
"improvement" module degraded results in combination. Single-number leaderboards
hide diagnostic failures.

## Scorecard against our design

**Already guarded by the debated architecture** (validated, not smug — each maps
to a documented failure): no single 0–100 score (pits 5, 9); atomic claims as
anchored rubrics (pit 1's root cause); deterministic evidence prep with
fail-loud gates (pit 4); `not_demonstrated` ≠ failed and evidence-cited verdicts
(pit 8's inspectability lesson); parsing-first engineering budget with visible
residual error (pit 3); embeddings for retrieval only (pit 6); fairness
guardrails incl. counterfactual testing (pit 7).

**New adoptions this research forces:**

1. **Judge verdicts are distributions.** Run each assessment k=3–5 times; report
   agreement; disagreement between runs auto-routes to `needs_review`. Bootstrap
   CIs for any aggregate. (Indeed's method, HackerRank's failure.)
2. **Calibrate the judge against humans.** A small human-labeled assessment
   sample with sensitivity/specificity correction joins the gold-set plan — raw
   judge agreement is not the true rate.
3. **Hard input gates codified:** empty/garbled extraction rejects before any
   LLM sees it (the John-Doe bug class); every judge output schema-validated
   with alerting, never warn-and-continue.
4. **Judge diversity caveat:** if multiple judge runs are used, same-lineage
   models share correlated errors — vary prompts/framings, not just seeds.
5. **"Why you matched" ships with counterfactual framing**, and we never use
   self-reported trust as evidence the explanation works.
6. **Fairness features need owners** — LinkedIn quietly sunset its
   representative-ranking feature; ours get re-audit entries in the benchmark
   suite, not launch-day-only checks.

## Verifier corrections applied

- Fortune's Amazon headline corrected (Reuters' wording had been cited); Paradox
  posts are 2025, not 2026; Resume-Matcher #260 is mid-project (2024), not "at
  project start," and discussion #362 is 2025.
- The practitioner lens's "mismatch" flags were confirmations with metadata
  notes (all headline numbers verified verbatim, incl. the 66–99 range and
  Indeed's 10.2-point correction).
- Textkernel/Affinda citations point at blog indexes rather than deep links —
  treated as vendor-positioning evidence, weakest tier here.

## References

### Products

- [Jobscan](https://www.jobscan.co) — match-rate mechanics; overstuffing warning
  in vendor copy.
- [Amazon](https://fortune.com/2018/10/10/amazon-ai-recruitment-bias-women-sexist/)
  — Fortune, 2018, on Reuters' reporting; model killed as un-fixable.
- [iTutorGroup settlement](https://www.eeoc.gov/newsroom/itutorgroup-pay-365000-settle-eeoc-discriminatory-hiring-suit)
  — EEOC, 2023; $365,000, five years oversight.
- [HireVue](https://en.wikipedia.org/wiki/HireVue) — facial-analysis
  discontinuation under EPIC/FTC pressure.
- [Eightfold responsible AI](https://eightfold.ai/trust/responsible-ai/) —
  audit-and-disclose posture (LL144 audits, ISO 42001).
- [LinkedIn Recruiter AI](https://www.linkedin.com/blog/engineering/recommendations/ai-behind-linkedin-recruiter-search-and-recommendation-systems)
  — 2019; embedding gains insignificant; sunset fairness feature.
- [AI 101](https://www.paradox.ai/blog/ai-101-the-ai-behind-our-assistant) —
  Paradox, 2025; candidate-manipulation handling.
- [Paradox AI 201](https://www.paradox.ai/blog/ai-201-how-paradox-builds-ai-guardrails-to-reduce-risk-and-bias)
  — 2025; component-level hallucination evals.
- [#260](https://github.com/srbhr/Resume-Matcher/issues/260),
  [#484](https://github.com/srbhr/Resume-Matcher/issues/484),
  [#791](https://github.com/srbhr/Resume-Matcher/issues/791),
  [#825](https://github.com/srbhr/Resume-Matcher/pull/825),
  [#902](https://github.com/srbhr/Resume-Matcher/pull/902),
  [#81](https://github.com/srbhr/Resume-Matcher/issues/81) — Resume-Matcher
  issue trail: parsing, CJK, John-Doe hallucination, silent drop, opacity.

### Research

- [ConFit](https://arxiv.org/abs/2401.16349),
  [ConFit v2](https://arxiv.org/abs/2502.12361),
  [ConFit v3](https://arxiv.org/abs/2605.09760) — the embedding-ceiling arc,
  2024–2026.
- [RJDB](https://arxiv.org/abs/2311.06383) — synthetic benchmark from a
  skill-occupation graph.
- [Wilson & Caliskan](https://arxiv.org/abs/2407.20371) — AIES 2024; 85.1% /
  11.1% / up-to-100% retrieval bias; length + name-frequency confounds.
- [FAccT 2026 follow-up](https://arxiv.org/abs/2606.22213) — human-AI
  collaboration layer; 55.6% review-time effect.
- [Correlated errors](https://arxiv.org/abs/2506.07962) — ICML 2025;
  same-lineage judges correlate, worsening with scale.
- [Context sensitivity](https://arxiv.org/abs/2507.08019) — company-framing
  moves screening outcomes (p<0.001); all models diverge from experts.
- [LL144 practitioners](https://arxiv.org/abs/2402.08101),
  [Null compliance](https://arxiv.org/abs/2406.01399),
  [Weak metrics](https://arxiv.org/abs/2302.04119),
  [Automating audits](https://arxiv.org/abs/2501.10371),
  [Assurance audits](https://arxiv.org/abs/2401.14908),
  [Supply chains](https://arxiv.org/abs/2604.22679) — the audit-regime
  literature.
- [EU AI Act Annex III](https://artificialintelligenceact.eu/annex/3/) —
  employment AI high-risk by default.
- [Counterfactuals](https://arxiv.org/abs/2505.14377) — XAI 2025; explanations
  reverse bias post-hoc; trust is a bad proxy.

### Practitioners

- [HackerRank ATS repro](https://danunparsed.com/p/hackerrank-open-source-ats) —
  Dan Kinsky, June 2026; 66–99 range; 65% spurious failure.
- [HN thread](https://news.ycombinator.com/item?id=48713832) — 1,032 points;
  temp-0 non-determinism confirmed at scale.
- [Bootstrap CIs](https://engineering.indeedblog.com/blog/2026/07/bootstrap-confidence-intervals-for-llm-evaluation/)
  — Indeed, July 2026; k=3–5 cluster bootstrap.
- [Calibration](https://engineering.indeedblog.com/blog/2026/08/calibrating-llm-based-population-estimates-with-human-validation/)
  — Indeed, August 2026; Rogan-Gladen, 10.2-point shift.
- [Textkernel blog](https://www.textkernel.com/blog/) — column-resume fixes
  decades in (index-level cite).
- [Affinda blog](https://www.affinda.com/blog) — 95% accuracy "not enough"
  positioning (index-level cite).
- [Mobley v. Workday](https://en.wikibooks.org/wiki/Professionalism/Mobley_v._Workday_and_the_Ethics_of_Algorithmic_Hiring)
  — vendor liability; collective certified 2025.
- [EEOC v. iTutorGroup](https://www.eeoc.gov/newsroom/eeoc-sues-itutorgroup-age-discrimination)
  — 2022 filing.
- [Stuffing threads](https://hn.algolia.com/api/v1/search?query=ATS%20keyword%20stuffing%20resume)
  — HN search anchor for the gaming arms race.
- [Searchlight](https://news.ycombinator.com/item?id=19273409) — YC W19; the
  resume-only-matching abandonment pivot.

---

Run: 6 agents (3 Sonnet finders @ effort high, 3 Opus verifiers @ effort high),
378k subagent tokens, ~US$3 (rough blended estimate).
