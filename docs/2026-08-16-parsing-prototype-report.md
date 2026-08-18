---
title: Parsing prototype report — job postings + LaTeX resume + match
date: 2026-08-16
type: report
status: current
---

# Parsing prototype: full results

End-to-end run of the debated data model on real inputs: two live postings
(fetched fresh from the Greenhouse `anthropic` and Ashby `ramp` boards,
2026-08-16) parsed into document nodes and tier-1 claims; a **fictional**
`moderncv` LaTeX resume parsed into contexts and evidence claims; and a
requirement-by-requirement match assessment joining the two. Parsing, profiling,
and the scoring floor ran deterministically (stdlib-only prototype scripts, zero
LLM calls); the tier-2 judging pass was then demonstrated by the assistant on
the prepared evidence.

> [!TLDR] The model survives contact with real documents
>
> 118 document nodes across two postings; 15 job claims now carrying **demand
> level** ("proficient in" vs "familiarity with") and **duration thresholds**;
> 26 resume evidence claims rolled into per-skill profiles of **how good, how
> long, and how recent** (job-time ≠ skill-time; React last used 66 months ago).
> Scoring runs in two tiers: a deterministic floor that computes every
> computable fact, and an LLM judge for the six case types the floor provably
> cannot decide — demonstrated here on real gaps (TypeScript⊃JavaScript
> equivalence, backend+frontend→full-stack composition, a B.Sc. the evidence
> layer missed). Nine findings changed the design.

## Coverage

Included: Greenhouse (HTML-escaped `content`) and Ashby (clean
`descriptionHtml`) — the two hardest of our eight source shapes; LaTeX resume
source parsing; deterministic matching with a demo policy (`v0`). Excluded from
this run: the other six sources, CJK content, tier-2 LLM extraction, the
concepts taxonomy (a 20-entry demo gazetteer stands in), and real resume data —
**all resume content below is fictional sample data**. Prototype scripts live in
the ephemeral job scratch dir (`parse_job.py`, `parse_resume.py`,
`match_demo.py`); promoting them into the repo is a pending decision.

## Stage 1 — job postings → nodes → claims

Pipeline: fetch → raw archive → unescape (Greenhouse-only gotcha) → HTML block
parse → NFKC normalize → section classification (heading gazetteer,
reset-on-unmapped) → tier-1 claim extraction (concept gazetteer + YOE regex +
OR-group heuristic + wording override).

### Greenhouse — Full-Stack Software Engineer, RL (id 5186067008)

60 nodes. Section map (headings / bullets / paragraphs):

| Section                | H   | •   | ¶   |
| ---------------------- | --- | --- | --- |
| about_company          | 1   | 0   | 1   |
| role_summary           | 1   | 0   | 5   |
| responsibilities       | 1   | 8   | 0   |
| requirements_required  | 1   | 9   | 0   |
| requirements_preferred | 1   | 8   | 0   |
| eligibility            | 1   | 0   | 7   |
| boilerplate            | 1   | 0   | 2   |
| other                  | 2   | 6   | 5   |

The `eligibility` block (heading "Logistics") isolated cleanly and contains the
facts-layer targets verbatim: "Minimum education: Bachelor's degree or an
equivalent combination…" (a degree-or-equivalent OR-group), "Visa sponsorship:
We do sponsor visas!…", and the hybrid-location policy. The compensation prose
(`$300,000—$405,000 USD` as a bare paragraph) landed in `other` — confirming the
`salary_in_text` fact extractor is required for Greenhouse.

Tier-1 claims (11) — now carrying the two dimensions the JD states, **how good**
(`demand_level`, from wording: "proficient in" / "experience with" /
"familiarity with") and **how long** (`min_months`, from in-bullet YOE):

| target           | importance | demand     | min_mo | group       | section                |
| ---------------- | ---------- | ---------- | ------ | ----------- | ---------------------- |
| domain:fullstack | required   | proficient | —      | —           | requirements_required  |
| skill:python     | required   | proficient | —      | or_group@19 | requirements_required  |
| skill:react      | required   | proficient | —      | or_group@19 | requirements_required  |
| skill:typescript | required   | proficient | —      | or_group@19 | requirements_required  |
| skill:gcp        | preferred  | working    | —      | or_group@30 | requirements_preferred |
| skill:aws        | preferred  | working    | —      | or_group@30 | requirements_preferred |
| skill:docker     | preferred  | working    | —      | or_group@30 | requirements_preferred |
| skill:cicd       | preferred  | working    | —      | or_group@30 | requirements_preferred |
| skill:llm        | preferred  | exposure   | —      | —           | requirements_preferred |
| skill:python     | preferred  | working    | —      | —           | requirements_preferred |
| skill:docker     | contextual | working    | —      | —           | other                  |

The demand ladder came straight from the real wording: "Are **proficient in**
Python…" → proficient; "**Experience with** cloud infrastructure" → working;
"**Familiarity with** LLM training" → exposure. Three levels from one posting,
deterministically.

Note the known tier-1 defect preserved on purpose: node 19 reads "proficient in
Python **and** a modern web stack (React, TypeScript, or similar)" — Python is
an AND conjunct, but the heuristic lumped it into the OR-group. Detecting
Boolean structure is tier-1; getting the tree right is tier-2.

### Ashby — Software Engineer, Frontend (Ramp)

58 nodes; requirements and benefits isolated; 27 nodes in `other` reflect Ramp's
many unmapped sub-headings (reset-on-unmapped keeps them honest rather than
bleeding). Claims (4):

| target            | importance | demand     | min_mo | section               |
| ----------------- | ---------- | ---------- | ------ | --------------------- |
| domain:frontend   | preferred  | working    | 24     | requirements_required |
| skill:javascript  | required   | proficient | —      | requirements_required |
| skill:react       | required   | proficient | —      | requirements_required |
| credential:degree | required   | working    | —      | requirements_required |

The `domain:frontend` row is the report's best single specimen: from "**Minimum
of 2 years** of frontend engineering experience **preferred**" (a bullet inside
the _required_ section) tier-1 extracted the concept, the 24-month threshold,
AND the wording override demoting it to preferred — three dimensions from one
line. The `credential:degree` row remains a **known false positive**
(short-token `\bba\b`-class regex match) — kept as the canonical example of why
tier-1 claims carry `assertion: inferred`.

## Stage 2 — LaTeX resume → contexts → evidence

Parsing the `.tex` source (never the PDF): `\cventry{dates}{title}{org}…` yields
contexts with dates for free; `\item` bullets yield evidence; verb choice sets
`evidence_mode` (led / built / operated / taught / used); title-only and
skills-list mentions are captured but explicitly weak.

Contexts (4) — all fictional:

| id  | type | title                   | org         | start   | end     | mo  | precision     |
| --- | ---- | ----------------------- | ----------- | ------- | ------- | --- | ------------- |
| 0   | job  | Senior Backend Engineer | Finlio      | 2021-03 | present | 66  | month/present |
| 1   | job  | Software Engineer       | Datamesh KK | 2018-07 | 2021-02 | 32  | month/month   |
| 2   | proj | Maintainer              | jobwatch    | 2024-01 | present | 32  | year/present  |
| 3   | edu  | B.Sc. Computer Science  | NTU         | 2014    | 2018    | 49  | year/year     |

Evidence claims (26):

| id  | target                     | mode     | strength      | ctx | mo  |
| --- | -------------------------- | -------- | ------------- | --- | --- |
| 1   | skill:go                   | built    | explicit      | 0   | 66  |
| 2   | skill:kubernetes           | built    | explicit      | 0   | 66  |
| 3   | skill:distributed-systems  | built    | explicit      | 0   | 66  |
| 4   | skill:python               | led      | explicit      | 0   | 66  |
| 5   | skill:postgres             | led      | explicit      | 0   | 66  |
| 6   | skill:kafka                | led      | explicit      | 0   | 66  |
| 7   | skill:distributed-systems  | led      | explicit      | 0   | 66  |
| 8   | skill:software-engineering | title    | title_derived | 0   | 66  |
| 9   | skill:python               | built    | explicit      | 1   | 32  |
| 10  | skill:gcp                  | built    | explicit      | 1   | 32  |
| 11  | skill:react                | built    | explicit      | 1   | 32  |
| 12  | skill:typescript           | built    | explicit      | 1   | 32  |
| 13  | skill:aws                  | operated | explicit      | 1   | 32  |
| 14  | skill:docker               | operated | explicit      | 1   | 32  |
| 15  | skill:cicd                 | operated | explicit      | 1   | 32  |
| 16  | skill:software-engineering | title    | title_derived | 1   | 32  |
| 17  | skill:python               | used     | explicit      | 2   | 32  |
| 18  | skill:sqlite               | used     | explicit      | 2   | 32  |
| 19  | skill:python               | listed   | weak          | —   | —   |
| 20  | skill:go                   | listed   | weak          | —   | —   |
| 21  | skill:typescript           | listed   | weak          | —   | —   |
| 22  | skill:kubernetes           | listed   | weak          | —   | —   |
| 23  | skill:terraform            | listed   | weak          | —   | —   |
| 24  | skill:aws                  | listed   | weak          | —   | —   |
| 25  | skill:gcp                  | listed   | weak          | —   | —   |
| 26  | skill:docker               | listed   | weak          | —   | —   |

The three-strength ladder is populated exactly as the claim grammar prescribes:
contextual bullets (explicit) > title-derived > bare skills-list (weak). Date
precision is recorded honestly — the education entry admits we only know years.

## Stage 3 — scoring in two tiers: deterministic floor + LLM judge

Direction set during the run: **scoring should be model-based, not rule-based.**
The architecture that survives that call: _the code prepares evidence; the model
judges._ The deterministic layer still computes every computable fact — merged
durations, recency, demand levels, thresholds — because those are facts the
model must not be allowed to invent; the LLM consumes the prepared claims and
evidence (with source spans) and produces the assessments. The reproducibility
tuple gains two members: model version and prompt/policy version. In the
product, the judging model is the user's own agent — the BYO-agent thesis
applied to scoring.

### 3a · Per-skill profiles: how good, how long, how recent

Rolled up from evidence, deterministically. Job-time ≠ skill-time: months merge
overlapping contexts; recency is months since the skill last appears.

| target                    | level      | months | last used | signals          |
| ------------------------- | ---------- | ------ | --------- | ---------------- |
| skill:python              | expert     | 98     | current   | led+built, scale |
| skill:go                  | proficient | 66     | current   | built, scale     |
| skill:kubernetes          | proficient | 66     | current   | built, scale     |
| skill:distributed-systems | proficient | 66     | current   | led+built        |
| skill:react               | proficient | 32     | 66mo ago  | built            |
| skill:typescript          | proficient | 32     | 66mo ago  | built            |
| skill:aws                 | proficient | 32     | 66mo ago  | operated         |
| skill:sqlite              | working    | 32     | current   | used             |

The profile levels are heuristic (mode + scale/outcome markers + duration) —
good enough to be a floor, not a verdict.

### 3b · Deterministic floor (policy v0, stale > 36mo)

Selected rows, both postings — `have` reads level/months/recency:

| status           | requirement                   | demand     | have              |
| ---------------- | ----------------------------- | ---------- | ----------------- |
| met              | GH python (required)          | proficient | expert/98/current |
| partially_met    | GH react (required)           | proficient | prof/32/stale-66  |
| partially_met    | GH typescript (required)      | proficient | prof/32/stale-66  |
| not_demonstrated | GH fullstack (required)       | proficient | —                 |
| partially_met    | GH aws+gcp+docker (preferred) | working    | prof/32/stale-66  |
| not_demonstrated | GH llm (preferred)            | exposure   | —                 |
| not_demonstrated | Ramp javascript (required)    | proficient | —                 |
| partially_met    | Ramp react (required)         | proficient | prof/32/stale-66  |
| not_demonstrated | Ramp degree (required)        | working    | —                 |
| not_demonstrated | Ramp frontend (pref, 24mo)    | working    | —                 |

`or_group@19` still satisfied (via python); `or_group@30` no longer fully
satisfied once staleness counts — the two-dimension floor is stricter and more
honest than the presence-only v1.

### 3c · LLM adjudication (tier-2), demonstrated on the floor's blind spots

The floor's `not_demonstrated` rows are not scoring errors — they are the
enumerated cases only judgment can decide. Adjudicated here by the assistant
from the prepared evidence (statuses stay inside the enum; deterministic facts
were inputs the model could not alter; every verdict cites evidence):

| requirement           | floor said       | judge says    | basis            | conf |
| --------------------- | ---------------- | ------------- | ---------------- | ---- |
| Ramp javascript (req) | not_demonstrated | partially_met | equivalent+stale | 0.80 |
| GH fullstack (req)    | not_demonstrated | partially_met | related          | 0.70 |
| Ramp degree (req)     | not_demonstrated | met           | direct           | 0.95 |
| GH llm (pref)         | not_demonstrated | partially_met | transferable     | 0.50 |
| Ramp frontend (pref)  | not_demonstrated | partially_met | related+stale    | 0.70 |
| GH react/ts (req)     | partially_met    | partially_met | direct+stale     | 0.75 |

Rationales, each grounded in evidence ids:

- **javascript:** TypeScript is a strict superset — proficient TS (ev 12, 32mo,
  built) implies JS competence; narrower→broader inference; staleness carries
  over. The floor cannot know type hierarchies; the concept-relations table plus
  a judge can.
- **fullstack:** 66 months senior backend (ev 1–7) composed with React/TS
  dashboard work (ev 11–12) is full-stack _range_ — a cross-evidence composition
  no per-concept rule can make; frontend half is stale, hence partial.
- **degree:** context 3 is a B.Sc. in CS — the floor missed it because the
  evidence layer never emitted a credential claim. An **extraction gap rescued
  by the judge reading contexts** — and a bug ticket for tier-1, not a scoring
  philosophy.
- **llm:** "Deployed models to production" (ev 13) is ML-adjacent, not
  LLM-specific — transferable at low confidence, flagged `needs_review` rather
  than silently counted.
- **frontend:** dashboard work (ev 11–12, 32mo) clears the 24-month threshold
  but ended 2021 — related evidence, stale.
- **react/ts:** the judge _agrees_ with the floor here and adds one nuance the
  bullet itself offers: "React, TypeScript, **or similar**" softens the
  requirement; staleness still holds. Agreement cases matter — the judge is
  calibrated by how often it must overturn.

## Findings that changed the design

1. Unmapped headings must **reset** section context, not inherit it — the bleed
   put Anthropic's salary and visa text under `requirements_preferred` until
   fixed.
2. Section is context, not truth — Ramp's self-labeled "preferred" bullet inside
   the required section; claim-level wording override adopted.
3. `eligibility` earns a section type — visa/education/location facts cluster
   there verbatim.
4. The tier-1/tier-2 boundary is now precise: rules detect Boolean structure and
   mis-parse it; LLM decomposition (on demand) owns the tree.
5. Short-token regexes produce confident false positives — tier-1 output must
   carry `assertion: inferred`.
6. Greenhouse comp-in-prose confirmed live — `salary_in_text` extraction is
   required, not hypothetical.
7. Ashby nests `<p>` inside `<li>` — flat block parsing breaks; the
   document-node **tree** is structurally necessary.
8. Job-time ≠ skill-time: crediting every skill with its context's full duration
   overstates; per-skill months are merged upper bounds, and **recency** is a
   first-class dimension (React proficient but 66 months stale reads very
   differently from React proficient and current).
9. The floor-vs-judge split is now empirical: rules decide presence, duration,
   recency, and demand-level arithmetic; the judge owns equivalence (TS⊃JS),
   composition (backend+frontend→fullstack), transferability (ML→LLM), and
   extraction-gap rescue — with agreement cases as its calibration signal.

## Not yet done / known limitations

- **Cross-bullet thresholds:** Ramp's 24-month threshold was captured only
  because its bullet contains its own concept (frontend engineering); a
  threshold in one bullet governing skills in another is still an unattempted
  tier-2 association task.
- The profile-level heuristic (mode + markers + months) is a stand-in; the LLM
  judge run here was the assistant inline, not yet a pipeline with structured
  output, validation against deterministic constraints, and stored match_runs.
- The 20-entry demo gazetteer found 13 claims; real coverage needs the concepts
  dimension with ESCO/Lightcast-scale aliases.
- Paragraph-form requirements inside requirement sections are scanned, but
  narrative requirements ("you have shipped systems that…") produce no claims
  without tier-2.
- No CJK inputs in this run — the JP/TW methodology (trigram FTS, NFKC before
  hashing) is designed but not exercised here.
- Resume is a fictional sample; rerun against the real `.tex` is one command.
- Group-level aggregation stops at OR satisfaction; weighted/conditional groups
  are unbuilt.

## Addendum 2026-08-17 — requirement expressions, faceted graph, gold set

> [!NOTE] Superseded later the same day
>
> The requirement-expression rule parser described here generalised 1/12 on
> unseen bullets; the current design is LLM-first extraction into an
> evidence-first demand profile with Markdown as the canonical text — see
> `2026-08-17-parsing-direction.md`.

The flat "one claim per keyword + `or_group`" model in Stage 1 above is
**superseded**. After the 2026-08-16 rulings (experimental/personal scope,
effort goes into parsing, extraction must not be keyword-only) the job side was
rebuilt as _requirement expressions_ and the code promoted into the repo at
`prototypes/parsing/` (see its `README.md` for the run commands and current
numbers).

What changed:

- **A bullet is one expression, not N claims.**
  `{op: SINGLE|AND|OR|MIXED, atoms[]}` with atom kinds
  `skill / family / abstract / capability / trait / credential`. Family atoms
  keep verbatim parenthetical `exemplars` and an `open_class` flag ("or similar
  / etc. / e.g."). Every atom carries a `span`; every requirement carries
  `assertion: inferred`. The old keyword list is now a projection
  (`flatten(expr)`), not the stored unit.
- **Rules flag what they cannot see.** Comma-scope inside noun phrases,
  contrastive clauses, unresolved multi-concept chunks, and bullets with no
  recognizable atom set `needs_tier2` instead of being shredded or silently
  dropped. On the 24 requirement bullets from these two postings: 16/16 tier-1
  trees exact, 8/8 tier-2 bullets flagged, 0 false flags.
- **Tier-2 is a pipeline now, not the assistant inline.** `retree.py` calls the
  local `claude` CLI in print mode with a JSON schema (bring-your-own-agent, no
  API key handling), validates op/atom-count consistency and rejects invented
  concept ids, retries once with the errors, and copies importance / demand /
  min_months through untouched — the judge cannot alter deterministic facts.
  First run (Sonnet, k=1, 2026-08-17): 7/8 exact vs gold at ~$0.05/bullet; the
  one miss is a labeled dispute (one trait vs two on Anthropic n24), kept as a
  disagreement rather than tuned away.
- **Concept graph gets kinds, facets, and typed edges** (user correction:
  "React, TypeScript, or similar" is not a homogeneous list — TypeScript is a
  language, React a framework). Families are satisfied per facet; evidence
  propagates along directed `superset_of` (full strength, TS→JS) and `implies`
  (capped one level below source, React→JS) edges; direct profiles always beat
  derived; derived ones carry a `via` chain. Demonstrated on the fictional
  resume: JavaScript went from `not_demonstrated` to proficient/32 mo derived
  via `typescript(superset_of)+react(implies)`. This piece is still in the
  scratch prototype (`facet_demo.py`), not yet in `prototypes/parsing/`.
- **A gold set exists** (`prototypes/parsing/gold/`, 24 bullets, labeling
  conventions in its README) and `eval_gold.py` exits non-zero on regression. It
  is far too small to be a benchmark; it is the diff harness every rule change
  now runs against, and the seed of the 300–500-bullet set the benchmark memo
  calls for.

Decision recorded on ML: a small extraction-side model layer (bullet-kind
classifier, heading classifier, span tagger + concept linker) is worth building
**after** the gold set reaches a few hundred labels, distilled from tier-2
outputs; scoring stays with the LLM judge. Rules keep parentheticals, YOE,
demand words, and `open_class` markers.

## Wrap-up

The debated model ran end-to-end on real postings, now with the two dimensions a
match actually turns on — how good and how long/recent — split correctly between
tiers: everything computable is computed (and the model may not alter it),
everything judgmental is judged (and must cite the computed evidence). The six
adjudication cases show the judge earning its place exactly where the floor is
provably blind, which is the strongest argument yet for the claims-grammar
design: it is what makes an LLM scorer _auditable_ instead of oracular.
