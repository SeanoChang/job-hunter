# prototypes/parsing — JD requirement parsing (stage A, superseded)

> **Status 2026-08-17: the rule-based tier-1 parser in `jobparse.py` is
> retired as an extractor.** It generalised 1/12 on unseen bullets and ~0 on a
> pasted Notion posting; the direction is now LLM-first extraction into an
> evidence-first demand profile — see `docs/2026-08-17-parsing-direction.md`.
> What is kept from here: the four fixtures, the `claude -p --json-schema`
> structured-output wiring in `retree.py` (`call_claude` takes system/schema),
> the eval-harness habit, and the 24-bullet gold as a **regression test for
> the development fixtures only** — not evidence of generalisation.

Stdlib-only prototype of the posting → nodes → requirement-expression pipeline,
with a hand-labeled gold set and an eval harness. Nothing here is packaged; run
the scripts from this directory with `python3`.

```text
fixtures/      two real postings (Anthropic Greenhouse 5186067008, Ramp Ashby 4e64ab86)
jobparse.py    tier-1 parser: HTML -> nodes+sections -> {op, atoms[]} per requirement bullet
gold/          24 requirement bullets labeled as expression trees (+ labeling conventions)
eval_gold.py   diff parser output vs gold; exit 1 on any tier-1 mismatch / unflagged tier-2
retree.py      tier-2: LLM re-treeing of flagged bullets via the local `claude` CLI
test_retree.py unit tests for retree's validator
silver/        judge outputs from dated runs (calibration artifacts, not truth)
```

## Run

```bash
python3 eval_gold.py -v                                # rules vs gold
python3 test_retree.py                                 # validator tests
python3 retree.py --out silver/$(date +%F)-sonnet.jsonl   # ~$0.05/bullet, flagged only
python3 eval_gold.py --silver silver/2026-08-17-sonnet-flagged.jsonl
```

## State on 2026-08-17 (development fixtures only)

| metric                                 | value                                |
| -------------------------------------- | ------------------------------------ |
| tier-1 exact expression tree           | 16/16                                |
| tier-2 bullets flagged (`needs_tier2`) | 8/8                                  |
| false flags on tier-1 bullets          | 0/16                                 |
| importance / demand / min_months       | 24/24 each                           |
| tier-2 judge (Sonnet) exact vs gold    | 7/8 (1 disputed label, see gold n24) |

## How a bullet flows

1. `build_nodes` flattens HTML into heading/bullet/para nodes; headings map to
   sections; an unmapped heading resets the section to `other`.
2. `parse_bullet` masks parentheticals same-length (offsets preserved), protects
   correlatives ("both … and"), splits on top-level `,` / `and` / `or`, and
   classifies each chunk: umbrella → `family`/`abstract`; one concept hit →
   `skill`; trait verb-phrase → `trait`; capability marker → `capability`;
   several hits with no umbrella head → `skill*`; else `other` (residue).
3. `op` is computed over classified atoms: one → `SINGLE`; explicit `or` only →
   `OR`; explicit `and` or bare comma list → `AND`; both → `MIXED`.
4. Routing flags: `no_atoms`, `mixed`, `comma_residue` (residue + a top-level
   comma), `unresolved` (`skill*`), `contrast` ("not just / rather than"). Any
   flag → `needs_tier2`.
5. `retree.py` sends flagged bullets to the judge with the tier-1 attempt, the
   kind definitions, and the known concept ids; output is schema-validated
   (`--json-schema`) and then checked by `validate()` (op/atom-count
   consistency, no invented ids, traits/capabilities carry no target); one retry
   with the errors appended. Importance / demand / min_months are copied from
   tier-1 — the judge cannot alter deterministic facts.

Every atom carries `span` into the normalized document text; every requirement
carries `assertion: inferred` and `provenance {node, span, node_hash, section}`.

## Known limits

- Concept alias table is a 25-entry seed; recall on unseen postings is low by
  construction. Growing it (or replacing it with a span tagger + linker) is the
  next step, gated on a larger gold set.
- Comma-scope inside noun phrases and contrastive clauses are _flagged_, never
  parsed, by rules.
- The judge fallback ran on one model with k=1; the design calls for k-sampling
  and disagreement → `needs_review`.
- No CJK inputs; no cross-bullet threshold association; no paragraph-form
  requirements.
