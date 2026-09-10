# Runbook — the local codex drain

status: current
owner-run: yes (needs the owner's codex login and `gh` auth; no store credentials)

The platform-key CI drain throttles; codex-cli (ChatGPT plan) does not, but it
only runs where its login lives. This pipeline extracts locally and uploads to
the store without any database or R2 credential on the machine: CI ships the
queue out as an artifact, the local script produces archive-format attempt
objects, and CI writes them into R2 where the runner's own catch-up scan
records and settles them.

## The loop

```bash
# 1. dump the queue (hash + markdown + next_attempt_no) as an artifact
gh workflow run extract-queue-dump.yml -f count=120
gh run download <run-id> --dir queue-dl

# 2. drain locally through codex-cli (model gpt-5.6-luna, runner-identical loop)
uv run python scripts/local_codex_drain.py queue-dl/queue-<run-id>/queue.jsonl outbox/ --max-docs 120

# 3. upload: encrypt the outbox, attach to a draft release, ingest
COPYFILE_DISABLE=1 tar czf outbox.tgz outbox
openssl enc -aes-256-cbc -pbkdf2 -pass file:$HOME/.config/job-hunter/outbox.key \
  -in outbox.tgz -out outbox.tgz.enc
gh release create outbox-<UTC-stamp> --draft --notes "codex outbox" outbox.tgz.enc#outbox.tgz.enc
gh workflow run outbox-ingest.yml -f tag=outbox-<UTC-stamp>
```

The ingest run ends with `extract run --max-docs 0` — the documented free-work
mode: catch-up replay and settle, zero engine calls. Its JSON summary's
`replayed` count is the number of attempts folded in.

## Which bundle a drain runs

`JOB_HUNTER_L2_BUNDLE` selects the engine tuple a run extracts under, from the
process env or `.env` like every other setting; an unregistered name fails at
startup rather than at the first document:

```bash
JOB_HUNTER_L2_BUNDLE=v1   # default — demand-profile/v5, schema 1, transforms.VALIDATOR_VERSION
JOB_HUNTER_L2_BUNDLE=v2   # demand-profile/v6, schema 2, validator/10 (l2/v2)
```

The tuple keys the queue, so a flip is a full re-extraction, never a resume:
every document re-queues under the new tuple and pays a fresh model call.

**The read surface does not follow the bundle yet** (verified 2026-09-10, and
the reason cutover is not a pure `.env` edit). `views.profile_row`,
`views.claims_view` and `pulse` all compute "the engine tuple in force" from
the v1 module constants — `l2.prompt.PROMPT_VERSION`, `l2.runner.SCHEMA_VERSION`,
`l2.transforms.VALIDATOR_VERSION` — not from `JOB_HUNTER_L2_BUNDLE`. So while
the bundle is `v2`:

- `q profile` / MCP `q_profile` keep serving the v1 row for any document that
  has one, because current-tuple rows sort first and v1 *is* the current tuple
  to that query; a document whose only row is v2 surfaces with
  `historical: true`.
- `q claims` filters `profile_mentions` on the v1 tuple, so mention rows
  written by a v2 run are invisible to it.
- `pulse`'s inline profile summaries come from `validated_profiles` scoped to
  the v1 tuple, so v2 extractions do not appear there either.

Nothing is lost — the v2 rows are in `extractions` and readable via
`extract show` — but a cutover that expects the read surface to move with the
writer needs those three call sites to take the tuple from the selected bundle
first. Treat that as a prerequisite of the flip, not a follow-up.

The setting governs `job-hunter extract run`, and therefore
`scripts/local_drain_loop.py`, which shells out to it. It does NOT reach
`scripts/local_codex_drain.py`: the outbox producer in the loop above still
imports the v1 prompt and `transforms.VALIDATOR_VERSION` directly, so the
CI-mediated pipeline stays on v1 until that script takes a bundle too.

### The A/B gate before flipping

Cutover is gated on live documents, never on the fixture benchmark alone — the
fixtures are the twelve curated contract cases; the gate is fresh queue
documents from the real corpus.

1. Pause the drain loop. One attempt writer at a time: a competing writer both
   contends for the advisory lock and skews the sample with `lock_held` skips.
2. Run the sample under v2 and read the JSON summary's
   `validated`/`quarantined`/`pending` counts:

```bash
JOB_HUNTER_L2_BUNDLE=v2 uv run job-hunter extract run --max-docs 20 --max-usd 0 -o json
```

3. Compare against v1's quarantine rate on the same documents (v1's per-tuple
   status counts come straight out of `extractions`).

**Pass** — v2 quarantine ≤ half of v1's, *and* the read path above takes its
tuple from the bundle: set `JOB_HUNTER_L2_BUNDLE=v2` in `.env` and restart the
drain loop; re-extraction proceeds newest-first. **Fail**: cutover halts, the
loop stays on v1, and the quarantined documents get a quarantine-class analysis
before anyone tries again. The switch never happens on the fixture suite alone.

Rolling back is selecting the previous bundle — set `JOB_HUNTER_L2_BUNDLE`
back to `v1` and restart. Never delete or relabel the rows the other tuple
wrote; both tuples coexist in `extractions` by design.

## Invariants and gotchas

- **Exactly one attempt writer at a time.** While this pipeline is active,
  `extract-backfill.yml` stays disabled, and the hourly fetch either stays
  disabled too or must not extract. Do NOT pause extraction by setting the
  `JOB_HUNTER_L2_MAX_DOCS` variable to 0 — Settings validation rejects 0 and
  the sync then fails BEFORE collecting (learned 2026-09-09, run 34412983679:
  one lost collection hour). The catch-up scan starts one second
  before the DB watermark; a CI-produced attempt recorded after a local
  attempt's timestamp but before its upload would put the local key behind the
  scan start, and it would never be recorded.
- **After a validator bump, replay before paying.** A bump re-queues every
  previously validated document. Run the $0 migration first (enable
  `extract-backfill.yml`, dispatch with `mode=rebuild`, disable again) so
  codex only sees documents that genuinely need a model.
- **Outcome parity is load-bearing.** `scripts/local_codex_drain.py` mirrors
  `runner._extract_doc` — outcome vocabulary, content retries, transport
  retries, the 5%/reprompt k-sampling. If the runner's loop changes, change
  the script in the same commit.
- **Only real attempt keys reach the archive.** macOS tar emits AppleDouble
  (`._*`) entries; `COPYFILE_DISABLE=1` suppresses them and the ingest
  additionally drops any key `parse_x_attempt_key` rejects. (The first ingest,
  2026-09-09, wrote 114 such junk keys under `extractions/attempts/`; they are
  inert — the scan's key regex skips them — and can be deleted from R2 if the
  clutter bothers anyone.)
- **The outbox is append-only and resumable.** Re-running the script skips
  documents already drained (key prefix match); re-running the ingest skips
  keys already in the archive. A throttled run (exit 3) uploads fine —
  partial progress is real progress.
- **`OUTBOX_KEY`** is the tarball's encryption secret: an Actions secret, with
  the local copy at `~/.config/job-hunter/outbox.key` (mode 600). Rotate by
  regenerating both.

## Turning it off

Re-enable the CI writers and this pipeline stands down cleanly:

```bash
gh workflow enable fetch.yml            # hourly collection (+ its extract step)
gh workflow enable extract-backfill.yml # the 6-hourly platform-key drain
```
