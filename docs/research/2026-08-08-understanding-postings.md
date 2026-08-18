# Research memo: extracting structure & meaning from job postings

*Sonnet research agent, 2026-08-08. Sourced via web search; verify licenses before bundling.*

## Key findings

**0. The ATS APIs already give away more than expected.** Lever has a structured
`workplaceType` enum (remote policy solved natively); Ashby's `?includeCompensation=true`
returns structured min/max/currency/interval; Greenhouse hides comp in `content` HTML.
Highest-ROI move: harvest native structured fields first, NLP only for the gaps.

**1. Skill extraction.** Maturity ladder: gazetteer matching (SkillNER, spaCy
PhraseMatcher) → supervised NER (SkillSpan benchmark: 14.5K sentences, NAACL 2022) →
taxonomy-aware embeddings → LLM extraction. Current SOTA pattern: **LLM-teacher →
small-local-student** (2025 NLDB work: GPT-4o-mini supervising a distilled mpnet
encoder, +15-25 pts R-Precision@5, mapped to ESCO).

**Taxonomies:**
- **ESCO** — ~13.9K skills, 3K occupations, multilingual, genuinely open license
  (EU 2011/833/EU), full download + local API Docker image. Best OSS default.
- **O*NET** — ~900 occupations w/ skill profiles, CC BY 4.0. US complement.
- **Lightcast Open Skills** — 32K+ skills, best tech-jargon coverage, but API is now
  contract-only; terms need review before OSS redistribution. Defer.

**Libraries:** SkillNER (spaCy, 60K terms), Nesta `ojd_daps_skills` (most
production-tested fully open pipeline; supports ESCO + Lightcast), esco-skill-extractor
(embedding-based), JobBERT/ESCOXLM-R (research encoders).

**2. Title normalization.** SOC (US), O*NET-SOC (finer), ESCO/ISCO-08 (EU) + published
crosswalks (incl. official ESCO↔O*NET). Practice: embedding/TF-IDF similarity against
ESCO preferred/alternative terms. JobBERT-V3.1 (2025) for multilingual.

**3. Seniority.** No standard taxonomy exists — genuine gap. De facto ladder:
intern → entry → mid → senior → staff/principal (IC) vs manager → director → VP (mgmt).
Practice: regex ladder rules on title + YOE cross-check + LLM for ambiguous. Build own
rule table + small eval set.

**4. Salary.** ~17-18 US states + DC now require posted ranges (CA/CO/NY/WA strictest;
remote postings judged by where work *could* be performed → employers disclose
everywhere). Salary is increasingly a structured-field read, not an NLP problem.
Fallback: regex first; reference architecture for hard cases: Draup `salary-normalizer`
(fine-tuned Gemma 3 270M on HF, 49 countries, JSON out) — the
small-dedicated-local-model pattern.

**5. Other fields reliability:** remote policy high (native/keywords), YOE medium-high
(regex), benefits medium (parse HTML <ul>, not NLP), visa low (model as 3-way:
sponsors / does-not / unstated; silence dominates).

**6. LLM extraction best practice:** provider-native structured outputs (strict
schemas); `instructor` library for multi-provider Pydantic; XGrammar for local
constrained decoding (now default in vLLM/SGLang). Failure modes: fabricated field
values, taxonomy drift (ground in exact label set), model-version drift (pin + re-eval),
prompt-satisfaction bias (make "unstated" a first-class value; prefer null over guess).

## v1 recommendations
1. Harvest native ATS structured fields first (zero NLP cost).
2. Regex/rules fallback for salary, YOE, remote.
3. ESCO as default taxonomy (+O*NET for US titles).
4. SkillNER or Nesta ojd_daps_skills as v1 skill extractor (local, no GPU).
5. Title normalization via ESCO term matching + curated title list.
6. Seniority via regex ladder + YOE cross-check.

**Defer:** per-posting LLM extraction as primary (use as enrichment/distillation),
Lightcast (license), fine-tuned NER, visa/benefits extraction, crosswalk chaining.

**Architectural principle:** every field gets a source-priority chain:
native ATS field → rules → distilled local model → optional LLM enrichment,
with null/"unstated" always preferred over guessing.
