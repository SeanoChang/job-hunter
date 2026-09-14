# Needs-review + quarantine failure analysis — full 475-doc set (validator 18)

Date: 2026-09-14. Scope: every document stuck under the v2 tuple
(demand-profile/v9, schema 2, validator 18) — 427 `needs_review` + 48
`quarantined` of 2,037 settled docs. Method: a full-coverage code sweep over
every archived attempt/audit/repair artifact for all 475 docs (no sampling),
followed by a 14-agent workflow (7 Sonnet finders, one per failure cluster; 7
Opus adversarial verifiers in fresh context). Every cluster claim below
carries its verifier-corrected form; the finders' original broader claims were
all downgraded to *partial*. Sweep scripts and per-doc classification JSON:
session scratchpad (`failure_sweep.py`, `failure_sweep.json`,
`disagreement_clusters.json`).

## The set at a glance

| slice | docs | signature |
| --- | --- | --- |
| quarantined | 48 | all k=1, zero surviving attempts: first sample's retry ladder exhausted |
| needs_review / incomplete cohort | 92 | ≥1 sample slot exhausted its attempt budget |
| needs_review / disagreement | 335 | k=3 complete, agreement gates failed (f1 335×, importance 165×, negation 110×) |

Document shape does **not** predict failure: median length 6.1–6.4k chars vs
5.4k for a 500-doc validated baseline, same non-ASCII fraction (~1%). The
failures below are content-handling logic, not hard documents.

## Confirmed findings (code-verified or adversarially verified)

### 1. The audit machinery itself blocks 61 review docs (14.3% of the queue)

61 of 427 review docs have no valid audit (`flags.audit` = error 23, not
_checked-after-retry 38), so they can never adjudicate. Two causes, both ours:

- **Hash-echo transcription.** `src/jobhunter/l2/v2/audit.py:237` makes the
  auditor retype the 64-hex candidate hash ("Echo the candidate hash back
  exactly as given"); codex garbles it by character transposition/deletion
  (`902116b1…` vs `902b116…`), and `judge()` (audit.py:577-581) hard-fails the
  audit. The echo has no diagnostic value — the prompt contains exactly one
  candidate. Code already knows the hash.
- **Audit evidence literal-substring failures** on curly quotes, unicode
  bullets (`・`), and paraphrase — the auditor's citations are held to the
  same verbatim rule as the extractor with none of the binder's fold tiers.

### 2. Deterministic derivation bug: unit evidence dropped (validator-code, confirmed)

`src/jobhunter/l2/v2/assemble.py:147-148` — `_derive()` calls
`derive_quantity(value, comparison)` for the experience/quantity families and
never forwards `evidence['unit']`, while the compensation branch (lines
154-159) does forward currency/period. Spec line 109 requires derivation "from
their cited evidence". When a sample follows the prompt's own instruction to
anchor the unit separately, the bare number defeats `_HAS_ALPHA` in
`facts.py` and an honest `present_unparsed` becomes a confidently wrong
dimensionless count: verifier reproduced `"12+"` + unit anchor `"years"` →
`{"dimension":"count",...}`. Drives part of the 141 `numeric_scope_unit`
blocking findings (10 findings across the 5 sampled docs split three loci:
this code bug, model errors, and grammar gaps).

### 3. Structural block accounting launders omissions (mixed, confirmed shape)

`verify.py::_check_accounting` is structural only — its docstring at
verify.py:371 says so ("Coverage is structural, never semantic recall"). Three
distinct laundering shapes among the 45 blocking omission findings in the
sampled cluster (omission is the top audit code corpus-wide: 400 blocking
findings):

- **(a) Checkable today:** a block dispositioned `statements` whose ref'd
  statements carry *zero* evidence entries from that block (14/45 findings) —
  e.g. 14 duty bullets all "accounted" to three statements evidenced from the
  intro paragraph. Deterministically detectable; v1 had exactly this check
  (`possible_omission`, validator/7) and v2 never ported it.
- **(b) Taxonomy gap:** whole sections consistently dropped as `context`
  across all 3 samples when their content doesn't map to the six statement
  kinds (KPI/success-metric lists, department-wide duty lists). Not sampling
  variance — every sample agrees on dropping them.
- **(c) Partial-block coverage:** only the first sentence of a multi-sentence
  block evidenced anywhere.

### 4. Retry collapse destroys relations (extractor-under-retry, confirmed dominant)

The relationship cluster's dominant mechanism is not the empty few-shot the
finder blamed: in 5 of 6 verified cases where a populated attempt failed
validation, the retry under prompt.py's "Fix ONLY these issues" instruction
returned with `relations` wholly or mostly deleted — including relations no
error named. Across the cluster's 22 generations, 55% of raw emits populate
relations; retries erase them. (The empty `relations` few-shot at
prompt.py:123 is a confirmed contributing factor; the emit schema does carry
the enums, so "zero template" was an overstatement — corrected by verifier.)

### 5. Importance is guessed where no modal keyword exists (prompt-contract, confirmed)

On qualification bullets with no modal keyword the label is unstable across
samples of the same document, and `importance_evidence` is backfilled with the
clause's own descriptor text ("Demonstrable expertise", "Strong creative
skills") rather than modality wording; `ambiguous` was used 0 times in 192
importance-bearing statements sampled. The disambiguation rules exist in the
spec (§3 heading-strength) but were never transcribed into the prompt bytes.
This is the engine behind the 202 importance blocking findings and most
importance-gate disagreements.

### 6. Proficiency-taxonomy overreach (confirmed half of unsupported_statement)

Generic strength adjectives ("Advanced", "Exceptional", "Strong", "Extensive
experience") are mapped onto the closed 4-value proficiency enum with the
adjective itself cited as `proficiency_evidence`, inconsistently within one
document. The auditor's line here is coherent (it accepts genuine taxonomy
words like "Fluency"); the extraction is wrong. The subject/kind half of the
finder's claim did not survive verification.

### 7. The mention layer has no floor (confirmed phenomenon)

`verify.py:_check_mentions` (349-365) checks surface-in-evidence and key
re-derivation only; nothing checks enumeration coverage ("Qt, Cocoa, React,
Angular, or similar" → one sample links 4/4, the medoid links 1/4), so mention
linkage swings wildly sample-to-sample and feeds both the 74 mention_linkage
blocking findings and F1 disagreement.

### 8. Attribution-failure mechanics (quarantine + incomplete, code-classified, full coverage)

Instance-level classification of every binder rejection across the 140
attribution-failed docs:

- `not a literal substring`: 169 wrong-block-but-text-exists-verbatim, 159
  paraphrase/hallucination, 38 normalization gaps (curly quotes/dashes), 7
  emphasis gaps. The wrong-block verifier **re-ran all 12 manifest entries
  through `assemble()` at HEAD (81f941c): only 9 of 23 errors now clear under
  the shipped re-anchor/fold tiers (cbc21b3, 835d590); 14 of 23 still fail** —
  the historical counts are partly stale, but a majority of the sampled
  failures reproduce today.
- `references:unknown_reference` (83 docs): one schema-comprehension failure
  dominates — `block_accounting.ref_ids` filled with mention ids (1,267
  instances) and area ids (392) where only statement/fact-entry ids are legal,
  persisting across retries even though the retry error names the expected
  types.
- `mentions:mention_ungrounded` (11 docs): 16 punctuation/case near-misses
  ("Wi-Fi"/"WiFi", a Cyrillic homoglyph `SPRМ`), 13 abbreviation mismatches,
  12 true hallucinations.
- Control-character corruption (2 docs): U+0001/U+0008/U+0013 in emitted
  strings — validator/17 rejecting as designed; these are engine corruption,
  correctly quarantined.

## Refuted / corrected in verification

All 7 finder claims returned **partial** (0 confirmed outright, 0 fully
refuted): the relationship few-shot causal story (retry collapse dominates),
the "3 of 13 populate relations" measurement (actually 12 of 22), the
unsupported_statement subject/kind half, the wrong-block "majority already
fixed at HEAD" claim (minority actually clears), and the omission cluster's
"one laundering mechanism" framing (three shapes, only one deterministically
checkable) were all corrected. Corrected forms are what appears above.

## Not covered

- Semantic deep-reads covered 35 stratified docs (5 per cluster) of the 335
  disagreement docs; the other 300 are covered by the quantitative sweep only.
- The 92 incomplete cohorts' *content* (beyond error mechanics) was not
  deep-read; the 281 `repair=dispute` docs' repair-quality question (why
  repairs reduce but don't clear blocking counts) was not analyzed.
- No fix has been implemented; everything above is diagnosis.

## Fix levers, ranked by expected clearance

1. **semantic-audit/v3**: drop the hash echo (code binds the hash), add the
   binder's fold tiers to audit evidence binding → unblocks adjudication for
   61 docs (14.3% of the queue) plus future audit reliability.
2. **validator/19 — forward unit evidence** in `assemble._derive()`
   (mirror the compensation branch) → kills the confidently-wrong dimensionless
   counts behind a large share of numeric_scope_unit findings.
3. **validator/19 — accounting evidence check**: `disposition="statements"`
   requires ≥1 ref'd statement with an evidence entry from that block (shape
   (a), deterministic); widen the requirement-language tripwire to `context`
   blocks as a warning (catches shape (b)).
4. **Retry contract fix**: the retry prompt must require preservation of
   valid content not named in the errors (or the validator flags a retry that
   deleted populated relations) → stops retry collapse.
5. **prompt v10**: transcribe spec §3 importance disambiguation; worked
   few-shot with populated relations + enumerated-alternatives mentions;
   anti-overreach rule for proficiency; a block_accounting example showing
   legal ref types (m-ids illegal — would address the single largest error
   bucket, 1,267 instances).
6. **Binder normalization tier** for curly quotes/dashes (38 instances) and
   canon-folded mention grounding (16 near-miss instances).
7. **Re-run the quarantine queue** against the current binder before further
   diagnosis — part of the historical error set is stale (9/23 sampled errors
   already clear at HEAD).
