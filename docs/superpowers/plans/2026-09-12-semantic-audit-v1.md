# semantic-audit/v1 Implementation Plan

> **For agentic workers:** implemented via a Workflow (implementation agents +
> adversarial verification), task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the spec §4 semantic auditor as a runner phase so v2 records
can clear `semantics`/`completeness`, become `search_eligible`, and so audited
cohorts with sampling disagreements are adjudicated instead of parked in the
human review queue.

**Architecture:** A new pure module `l2/v2/audit.py` (prompt, closed finding
schema, finding validation, dimension/severity mapping) + an audit phase in
the runner that runs after sample collection and BEFORE settlement (spec §6:
no publication mid-audit, no auto-promotion after a terminal state). The audit
artifact is archived at a key derived from the audited candidate's attempt
key; `settle` reads it through an `audit_hook`, so live, catch-up, and replay
fold identically and no DB migration is needed. Settlement policy changes ⇒
`validator/16` (bump, never edit in place).

**Tech stack:** existing codex-cli engine path, archive store, psycopg store.
No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-07-parsing-contract-v2-design.md`
(§4 Auditor, §5 flow, §6 quality/lifecycle). Repair (`semantic-repair/v1`) is
explicitly OUT of this increment.

## Global constraints

- Frozen identifiers: new `AUDIT_VERSION = "semantic-audit/v1"`; v2 bundle
  becomes (`demand-profile/v9`, `2`, `16`). v1 untouched. Never edit frozen
  bytes in place.
- All new modules in `l2/v2/` stay pure: no I/O, no model calls, no env.
- `mypy --strict`, ruff line 100. Tests offline and deterministic.
- Spec §6 invariants: `no_findings` ≠ certification; audit_error is never a
  pass; blocking findings gate eligibility, never silently drop data; human
  rejection is final.

## Policy (normative for this increment)

Settlement under a bundle with `audit_version` set, cohort complete:

| agreement | audit outcome | status | sampling dim | search_eligible able? |
|---|---|---|---|---|
| pass | no blocking, both dims `no_findings` | validated | complete/not_requested | yes |
| pass | blocking findings | validated | complete | no (findings visible in quality) |
| pass | absent or error | validated | complete | no (`not_checked`/`error`) |
| fail | no blocking, both dims `no_findings` | **validated** (adjudicated) | `adjudicated` | yes |
| fail | blocking, error, or absent | needs_review | disagreement | no |
| sample_failed (incomplete cohort) | any | unchanged from validator/15 behavior | incomplete | no |

Audit findings never demote an agreeing, structurally-valid cohort — they gate
eligibility only. Adjudication rationale: a full-source semantic audit of the
medoid outranks sampling variance (spec: "Sampling is not a substitute for
semantic audit" — and symmetrically, variance alone is not a semantic defect
when the audited candidate is fully source-supported).

Codes → dimension: `omission`, `source_insufficiency`, `bad_exclusion` →
completeness; `unsupported_statement`, `importance`, `polarity_subject`,
`relationship`, `numeric_scope_unit`, `mention_linkage` → semantics;
`wording_redundancy` → semantics, the only **warning** code. All other codes
are **blocking**. Severity is code-owned (derived from the code), never taken
from the model.

---

### Task 1: `l2/v2/audit.py` — the pure auditor contract

**Files:** Create `src/jobhunter/l2/v2/audit.py`, `tests/l2/v2/test_audit.py`.

**Interfaces (produces):**
```python
AUDIT_VERSION = "semantic-audit/v1"
TEMPLATE: str  # spec §4 auditor text, verbatim, + rendering scaffold
def render(markdown: str, candidate_hash: str, record: dict[str, Any]) -> str
    # numbered blocks/1 listing (reuse source.annotate) + candidate JSON
def emit_schema() -> dict[str, Any]
    # strict engine-facing schema: {"findings":[{code, targets:[str],
    #  evidence:{block_id,text,occurrence}|null, explanation}],
    #  "unresolved":[{question, targets:[str]}], "candidate_hash": str}
    # codes as a closed enum; explicit "type" on every union member (codex
    # strict-mode lesson from emit_guard.py)
SEVERITY: dict[str, str]           # code -> "blocking" | "warning"
DIMENSION: dict[str, str]          # code -> "semantics" | "completeness"
@dataclass(frozen=True)
class AuditOutcome:
    semantics: str      # no_findings | findings | error
    completeness: str   # no_findings | findings | error
    blocking: int
    warnings: int
    findings: list[dict[str, Any]]    # validated, evidence bound
    unresolved: list[dict[str, Any]]
class AuditJudgeError(Exception): ...  # message lists concrete defects
def judge(emit: dict, record: dict, markdown: str, candidate_hash: str) -> AuditOutcome
```

`judge` validity rules: `candidate_hash` must equal the supplied hash; every
target id must exist in the record (statement/fact/mention/area/relation ids);
a finding's evidence, when present, must bind via `source.resolve(...,
lenient=True)` (grounding needs existence); an `omission` or
`source_insufficiency` finding REQUIRES evidence (spec: "a missing statement
needs a source citation"); unknown code → invalid. Any violation raises
`AuditJudgeError` (→ the caller records an error outcome, never a pass). An
unresolved question targeting a blocking dimension counts toward `blocking`
(spec: "an unresolved classification affecting those blocking dimensions is
also blocking").

- [ ] Write failing tests: template pins (AUDIT_VERSION, spec sentences
      present: "This is not certification.", "Do not issue accept/promote/
      retry commands"), emit-schema shape + closed code enum, `judge` happy
      path (empty findings → both dims no_findings, 0 blocking), each
      validity rule rejecting, severity/dimension mapping, unresolved-counts-
      as-blocking, evidence binding through typo/emphasis tiers.
- [ ] Implement; run `uv run pytest tests/l2/v2/test_audit.py -q` to green.
- [ ] `uv run ruff check . && uv run mypy` clean. Commit.

### Task 2: versions, policy, and plumbing seams

**Files:** Modify `src/jobhunter/l2/v2/facts.py` (VALIDATOR_VERSION 15→16 +
history line), `src/jobhunter/l2/v2/quality.py`, `src/jobhunter/l2/state.py`,
`src/jobhunter/l2/bundles.py`, `src/jobhunter/archive/keys.py`.
Tests: `tests/l2/test_state.py` (extend), `tests/l2/v2/test_quality.py`
(extend), `tests/archive/test_keys.py` (extend), `tests/l2/test_bundles.py`.

**Interfaces (produces):**
```python
# archive/keys.py
def x_audit_key(attempt_key: str) -> str
    # "extractions/attempts/…" -> "extractions/audits/…"; raises ValueError
    # on a non-attempt key
# quality.py
_SAMPLING_OK = ("not_requested", "complete", "adjudicated")
# state.py
AuditView = …  # minimal protocol/dataclass the hook returns:
               # semantics, completeness, blocking (int)
def derive_state(attempts, reviews, globs, cohort_hook,
                 audit_hook: Callable[[str], AuditView | None] | None = None)
# bundles.py — Bundle gains optional fields (None ⇒ no audit phase; v1 stays None)
audit_version: str | None = None
audit_render: Callable[[str, str, dict], str] | None = None
audit_emit_schema: Callable[[], dict] | None = None
audit_judge: Callable[[dict, dict, str, str], Any] | None = None  # audit.judge
```

`derive_state` implements the policy table above; `audit_hook` is called with
the chosen candidate's attempt key only when a hook is supplied AND the
decision needs it. No hook ⇒ exactly today's validator/15 behavior (v1 path
byte-identical).

- [ ] Failing tests first: policy table as parametrized cases (each row),
      v1-no-hook regression (existing tests untouched and green),
      `x_audit_key` mapping + rejection, sampling `adjudicated` eligibility
      in `quality.assess`, v2 bundle registers audit fields + validator "16",
      `get_bundle_for_tuple("demand-profile/v9","2")` still resolves.
- [ ] Implement; targeted tests green; full `uv run pytest -q` green;
      ruff+mypy clean. Commit.

### Task 3: the runner audit phase + settle wiring + serving

**Files:** Modify `src/jobhunter/l2/runner.py`, `src/jobhunter/l2/v2/serve.py`
(only if quality injection needs it), `src/jobhunter/l2/v2/project.py` (no
change expected — already gates on `record["quality"]["search_eligible"]`).
Tests: `tests/l2/test_runner_v2.py` (extend).

**Consumes:** Task 1 `render/emit_schema/judge/AuditOutcome`, Task 2 seams.

Runner (per-document, after sample collection, before settle):
1. Compute the cohort agreement over in-hand records (`agreement.agree`,
   f1_min from the bundle) to pick the medoid candidate — the SAME choice
   settle will make (determinism pinned by a test).
2. Render the audit prompt for the medoid's record; call the engine with
   `audit_emit_schema()`; one transport retry (mirror `_take_samples`).
3. Archive the full audit artifact (raw response, parsed findings or the
   `AuditJudgeError` text, AUDIT_VERSION, candidate attempt key + candidate
   hash, tokens, model) at `keys.x_audit_key(candidate_attempt_key)` —
   write-once, before any DB write (archive-first, [A1] discipline: archive
   I/O outside transactions).
4. Engine failure after retry / invalid JSON / `AuditJudgeError` ⇒ archive an
   `audit_error` artifact. Never a pass, never blocks settlement.
5. `settle` builds `audit_hook`: probe `store.exists(x_audit_key(k))`, load,
   return the `AuditView` (or None). Catch-up and rebuild therefore fold
   audited documents identically with zero extra model calls.
6. The stored profile must carry post-audit truth: before `profile_of`/
   `mention_rows`, settle rewrites the chosen record's `quality` via
   `quality.assess(...)` with the audit dims, sampling state, and blocking
   count. An adjudicated-or-agreeing, no-blocking, audited record therefore
   serves `search_eligible: true` and REAL mention rows — the first ever.
   k=1 (unsampled) docs get audited too: eligibility must not require
   sampling.

- [ ] Failing tests first (scripted engine, offline): (a) agreeing cohort +
      clean audit ⇒ validated, eligible, mention rows non-empty; (b)
      agreeing cohort + blocking finding ⇒ validated, ineligible, findings
      in profile quality; (c) disagreeing cohort + clean audit ⇒ validated,
      `sampling: "adjudicated"`; (d) disagreeing cohort + audit error ⇒
      needs_review; (e) audit artifact archived before the extractions row
      commits; (f) re-settle from a cold catch-up reproduces (a)–(d) with no
      engine calls; (g) k=1 doc audited and eligible.
- [ ] Implement; `uv run pytest tests/l2/ -q` green; full suite + ruff +
      mypy clean. Commit.

### Task 4: live verification + the review-queue campaign

- [ ] `scripts/live_smoke` style dry-run: 3 documents end-to-end locally
      (codex), inspect archived audit artifacts and quality blobs by hand.
- [ ] Re-run the 113 ever-quarantined set fresh under (v9, 2, 16) with the
      experiment harness (no retry boundaries needed — fresh tuple), report
      validated/adjudicated/review/quarantine split and eligibility counts.
- [ ] Report honestly: adjudicated ≠ verified-correct; spot-check ≥5
      adjudicated docs' audit artifacts before claiming the queue cleared.

## Non-goals

- `semantic-repair/v1` and re-audit (next increment; blocking findings stay
  visible-but-ineligible).
- The served-tuple/views cutover and rebuild.py bundle-awareness (standing
  open work, tracked separately).
- Any F1/threshold recalibration and the containment-aware alignment change
  (needs the adjudicated comparator this increment produces).
