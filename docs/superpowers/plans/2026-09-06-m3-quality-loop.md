# M3 Quality Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The L2 harness's M3 increments — k-sampling with code-computed
agreement, the demote-only refuter, and the weekly consolidation artifacts —
exactly as `docs/2026-08-26-l2-extraction-harness.md` §2, §4.4–4.6 specifies.

**Architecture:** A pure agreement module (no I/O, no LLM) feeds the existing
runner: audit/escalation documents run k=3, everyone else k=1; the consensus
record is the medoid sample chosen whole; disagreement demotes to
`needs_review` with the report stored in `extractions.agreement`. The refuter
is an engine pass over validated rows that can only demote, its verdict
archived before the row moves. Consolidation reads validated rows only and
emits append-only artifacts on a weekly schedule.

**Tech Stack:** existing (`l2/` package, psycopg, the engines already built).

**Spec:** docs/2026-08-26-l2-extraction-harness.md (§4.4 reviews/refuter,
§4.5 k-sampling/agreement, §2 consolidation). Schema columns (`sample_slot`,
`k`, `agreement`, `chosen_attempt`) shipped in M2 — no schema change.

## Global Constraints

- Agreement is computed by code, never an LLM (spec §4.5).
- Thresholds live under `VALIDATOR_VERSION`; changing one bumps it.
- The refuter and any judge demote or annotate, never promote (§4.4).
- Samples are never merged: `chosen_attempt` points at one archived response.
- Review/refuter events append to the archive BEFORE any derived row moves.
- All writes under `EXTRACT_LOCK_KEY`; caps count observed usage.

## Backbone

### Task 1: pure agreement module (`l2/agreement.py`)

Claim alignment across samples by span-overlap Jaccard ≥ 0.5 (greedy,
one-to-one); mean pairwise claim-set F1 ≥ 0.80; importance agreement on
required claims ≥ 0.90; zero negation disagreements (any split escalates
unconditionally). Medoid sample selection (whole record, max mean pairwise
F1, deterministic tie-break by sample slot). Disagreement report dict for
`extractions.agreement`. Pure functions over profile dicts; exhaustive tests.

### Task 2: k-sampling in the runner

Slot rule: `int(document_hash[:8], 16) % JOB_HUNTER_L2_AUDIT_MOD == 0`
(default 20) → k=3; a slot-1 pass that needed a reprompt escalates to k=3;
everyone else k=1. Extra samples are ordinary archived attempts under their
`sample_slot`. Agreement gate on completion: pass → validated with the medoid
as `chosen_attempt` and the report in `agreement`; fail → `needs_review` with
the report. Caps count all samples.

### Task 3: demote-only refuter

`extract refute` verb + runner hook: an engine pass re-reads a validated
row's document and claims and returns refute/uphold with cited reasons; a
refute verdict appends the review event to `reviews/` then demotes the row to
`needs_review`. Uphold writes provenance only. Never promotes, never touches
non-validated rows.

### Task 4: weekly consolidation

`extract consolidate`: reads validated rows only; emits append-only artifacts
to the archive — drift report (agreement series, quarantine rates by
source/board, validator-version mix), the human audit queue (needs_review
oldest-first with reasons), and refuter summary. A weekly cron step in the
existing workflow family (no new scheduler); exit 0 on nothing-to-do.
