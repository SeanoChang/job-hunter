# prototypes/parsing — retired rule-based parser (reference only)

The rule-based tier-1 JD parser is **retired as an extractor** (banner in
`README.md`, ruling 2026-08-17): it generalised 1/12 on unseen bullets. The
direction moved to LLM-first extraction into an evidence-first demand profile
(`docs/2026-08-17-parsing-direction.md`). Nothing here is packaged or linted
(excluded in `pyproject.toml`); kept as reference and regression fixtures.

What survives from here:

- `fixtures/` — four real postings used as development fixtures.
- `retree.py` — the `claude -p --json-schema` structured-output wiring
  (`call_claude` takes system/schema), reused habit for LLM calls.
- `gold/requirement_bullets.jsonl` + `eval_gold.py` — the eval-harness pattern
  and 24-bullet gold (valid for these fixtures only, not generalisation
  evidence).
- `silver/` — judge outputs from dated runs; calibration artifacts.

Run scripts from this directory with `python3`; see its README.

Parent: [../CLAUDE.md](../CLAUDE.md)
