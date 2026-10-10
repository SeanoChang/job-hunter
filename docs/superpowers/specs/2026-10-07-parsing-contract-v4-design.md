# Parsing contract v4 — sponsorship, typed mentions, tracks

Status: **approved 2026-10-07** (Sean). Amends
`2026-09-22-parsing-contract-v3-design.md`; every section of v3 (and of v2
through it) not named here stands. Engine identifiers: prompt
`demand-profile/v12`, schema `4`, validator `21`, `semantic-audit/v5`,
`semantic-repair/v3`, registered as bundle `v3`. Bundle `v2` (v11/3/20) stays
registered for rollback.

## 1. Why

A read of five entry-level postings through the local MCP server on
2026-10-07 (v11 records, 162 validated internship and new-grad SWE/ML
documents) found four gaps the reader hits on the first posting:

1. **Sponsorship has no field.** Visa's "Visa will not sponsor applicants for
   work visas in connection with this position" is a `hiring_policy`
   statement with `polarity: negative`. It is extracted correctly, but nothing
   exposes it as a value, so an international student cannot filter on it.
   Anduril's "Must be a U.S. Person due to required access to U.S.
   export-controlled information" has no home at all.
2. **Skills are missed in duty lines.** "Self-serve deployment of Kafka
   clusters using Docker and Kubernetes" became a responsibility statement
   with no mentions. In a 13-posting sample, 81% of mention links attach to
   qualification statements and 7% to responsibilities, while
   responsibilities are 29% of statements.
3. **Mentions are untyped.** A mention is any name the model linked, so the
   "skills" a reader sees include places (Toronto, Montreal), the employer
   (Lyft, Visa), dates (Summer 2027), degrees (Bachelor's) and fields of study
   (Computer Science). The serving filter added on 2026-10-07 (skills only
   from qualification or responsibility statements) removes most of these but
   cannot remove a location inside a qualification line.
4. **Tracks are flattened.** Figma's "When you apply, you'll tell us which
   areas you're most interested in" lists four tracks (Product,
   Backend/Infrastructure, Security Engineering, Open). The record turned three
   of them into unconditional duties and dropped "Open".

All four need the model to anchor new evidence, so they share one contract
bump and one re-extraction.

## 2. Contract deltas (schema 4, prompt `demand-profile/v12`)

### 2.1 Authorization presence

`facts.presence` gains three families, beside `experience`, `compensation`,
`quantities` and `dates`:

| family | what the model anchors | example quote |
| --- | --- | --- |
| `sponsorship` | the sentence that states a visa-sponsorship policy | "Visa will not sponsor applicants for work visas" |
| `citizenship` | the sentence that restricts eligibility by citizenship, U.S.-person status, clearance or export control | "Must be a U.S. Person due to required access to U.S. export-controlled information" |
| `work_authorization` | a work-authorization requirement that says nothing about sponsorship | "Must be authorized to work in the US" |

Each entry is the existing presence shape plus a bound polarity:

```
{ state: stated | none_found | unresolved,
  evidence: [ref] | null,           # bound like every reference
  polarity: positive | negative | ambiguous | null,
  polarity_evidence: [ref] | null } # the negation or modal words, verbatim
```

`polarity` is set only for `sponsorship` and `citizenship`. It is the
model's reading of whether the policy is granted (positive: "we sponsor H-1B",
"sponsorship may be available") or refused (negative: "will not sponsor",
"unable to sponsor now or in the future"). For `citizenship`, positive means
the restriction applies.

### 2.2 Derived authorization (code-owned)

Assembly derives one `authorization` object from the three presence entries.
The model never emits it.

| field | value | rule |
| --- | --- | --- |
| `sponsorship` | `yes` | `sponsorship.state = stated` and `polarity = positive` |
| | `no` | `sponsorship.state = stated` and `polarity = negative` |
| | `undeclared` | anything else: `none_found`, `unresolved`, or `ambiguous` polarity |
| `citizenship_required` | `true` | `citizenship.state = stated` and `polarity = positive` |
| | `false` | anything else |
| `evidence` | quotes | the sponsorship, citizenship and work-authorization evidence, each when present |

Rulings (Sean, 2026-10-07):

- Only an explicit sponsorship sentence sets `yes` or `no`. "Must be
  authorized to work in the US" alone leaves `sponsorship: undeclared`; its
  quote is still carried in `evidence.work_authorization`.
- Citizenship is its own field, never folded into `sponsorship: no`. The
  international-student filter is `sponsorship = no OR citizenship_required`.
- Absent evidence is `undeclared`, never a guess. An `ambiguous` polarity is
  also `undeclared`, with its quote kept so the reader can judge.

### 2.3 Mention types

Each mention gains a required `type`, emitted by the model:

| type | covers | examples |
| --- | --- | --- |
| `skill` | languages, tools, frameworks, platforms, methods | Python, Kafka, Docker, PyTorch, REST |
| `field_of_study` | academic fields | Computer Science, Computer Engineering |
| `credential` | degrees, certifications, licences | Bachelor's, CPA, AWS Certified |
| `location` | places | Toronto, San Francisco |
| `organization` | employers, customers, institutions | Lyft, Visa |
| `other` | dates, programmes, anything else | Summer 2027 |

The `role` field (`direct`, `example`, `contextual`) stays. It says how a
name is used in its sentence; `type` says what kind of thing it is.

### 2.4 Mention recall

The prompt's mention rule becomes: every named technology, tool, language,
framework, platform or method in **any** statement is a mention, including
responsibility lines and example-project lists ("projects could include").
The prompt gains one worked example built on a responsibility line. Names
are never inferred from titles (unchanged).

### 2.5 Tracks

`relations` gains `tracks`, for postings where one requisition covers several
kinds of work and the candidate is placed on one:

```
tracks: null | {
  selection: candidate_choice | team_match | unstated,
  selection_evidence: [ref] | null,   # "When you apply, you'll tell us which areas"
  items: [{
    id, name_evidence: [ref],         # "Backend/Infrastructure"
    evidence: [ref],                  # the whole list item
    open: bool,                       # "I'm flexible and still exploring"
    statement_ids: [...],             # statements that hold only inside this track
    mention_ids: [...]                # distributed systems, developer tooling
  }]
}
```

A statement linked to a track holds only inside it. Statements outside every
track apply to all of them (unchanged meaning). `tracks` is null when the
posting describes one kind of work.

### 2.6 Currency (no derivation change)

`facts.py` keeps `$` unresolved by design: it is USD, CAD, AUD and others.
The prompt asks the model to anchor the `currency` aspect when the same block
names a currency code ("$41 to $48 USD"). The derivation grammar is
unchanged. When two compensation entries state the same range, the digest
reports the one with a currency.

## 3. Settlement (validator `21`)

The gate keeps v3's two checks and extends **negation** to the two new
polarities. A cohort that reads "will not sponsor" as `positive` in one sample
and `negative` in another is the error that harms the reader most, so it
gates like any other negation split.

Reported as metrics, never gating: mention `type` splits, track membership
splits, `selection` splits.

One new verifier finding, a **warning** and never a gate: a `skill` mention
linked only to `employment_constraint`, `compensation_statement`,
`employer_context` or `hiring_policy` statements (the Toronto case under the
new contract).

## 4. Audit (`semantic-audit/v5`)

v4's codes stand. `omission` now explicitly covers a named technology in a
responsibility line that has no mention, and `bad_exclusion` covers a track
list that was not recorded as `tracks`. The 26,595 v11 records were produced
by the offline v3 migration and none of them was audited
(`quality.semantics = not_checked` on all of them). The plan must confirm the
drain audits new v12 extractions before the corpus re-extracts.

## 5. Serving

- **Digest** (`profile` without `--full`, `pulse` inline): adds
  `authorization` (`sponsorship`, `citizenship_required`, and one quote
  each), `skills` (mentions with `type: skill`, bounded, with an omitted
  count), `education` (`field_of_study` and `credential`), and `tracks`
  (names, `open`, `selection`). The schema-3 statement-kind skill filter
  does not apply to schema 4.
- **Filter.** A derived, rebuildable table holds one row per
  `(document_hash, engine tuple)` with `sponsorship` and
  `citizenship_required`. It is written by the same store path as
  `profile_mentions` and rebuilt by `extract rebuild`. The migration that
  creates it was approved on 2026-10-07.
- **Verbs.** `q postings --sponsorship yes|no|undeclared` and
  `--citizenship-required true|false`; the same arguments on the MCP
  `postings` tool; `authorization` on `pulse` events. A posting whose current
  document has no extraction under the tuple in force reports
  `sponsorship: null` (not extracted), never `undeclared`, and no
  `--sponsorship` value matches it.
- **Requirement** (validator `24`, 2026-10-10). Each schema-4 claim-index
  area and claim carries `requirement`: `required`, `preferred` or null.
  Code reads it from two quotes the record already binds — the statement's
  `modality_evidence` first, then its `section_heading` — with the fixed
  lexicon in `facts.derive_requirement`. A quote that names both strengths,
  or neither, gives null. This is not the model verdict contract v3 removed:
  every sample of a posting reads the same quotes, so every sample gets the
  same answer. `profile_mentions.importance` carries it, so
  `q claims --importance required|preferred` selects schema-4 rows. The claim
  `importance` stays the no-verdict sentinel, so the agreement gate is
  unchanged. On 3,000 validated v15 documents it labels 62% of
  qualification statements; the rest sit under headings such as "What we
  look for".
- **Experience floors** (validator `24`). A fact's cited comparison that is
  the value's own plus sign (`+`, `3+`) or `or more` derives `gte`, and a
  `gte` phrase beside a plus value (`Minimum` · `3+ years`) agrees with it.
  72% of the corpus's 52,133 unparsed experience facts had this shape.
  Schemas 2 and 3 keep validator `20`'s grammar.

## 6. Migration and re-extraction

Schema 3 cannot derive schema 4 offline: mention types, tracks and the
authorization presence all need the model to anchor new evidence. Bundle
`v3` therefore re-extracts. Codex on the ChatGPT plan bills $0, so the cost
is time.

Order:

1. Register bundle `v3`. Give `scripts/local_codex_drain.py` a bundle
   argument (it imports the v1 prompt directly today, so the outbox path
   cannot feed Neon any newer contract).
2. Re-extract the local store under `v3`, entry-level postings first (the
   target slice: internship and new-grad SWE/ML).
3. Ship the outbox to Neon. Production switches its read path
   (`JOB_HUNTER_L2_BUNDLE`) only after its rows exist (decision 2026-10-07).

Rollback is selecting bundle `v2`. No row of either tuple is deleted.

## 7. Regression contracts

New fixture cases, each written to fail before the change:

| case | source | must hold |
| --- | --- | --- |
| V1 | Visa sophomore SWE intern | `sponsorship: no` with the "will not sponsor" quote; Kafka, Docker, Kubernetes are `skill` mentions linked to the responsibility |
| V2 | Lyft SWE intern, ML, Toronto | no `location` or `organization` among skills; Computer Science is `field_of_study` |
| V3 | Figma SWE intern | four tracks, `Open` with `open: true`, `selection: candidate_choice`; Backend/Infrastructure carries distributed systems and developer tooling |
| V4 | Anduril flight software intern | `citizenship_required: true`; `sponsorship: undeclared` |
| V5 | synthetic: "Must be authorized to work in the US" only | `sponsorship: undeclared`; `evidence.work_authorization` holds the quote |
| V6 | synthetic: "Sponsorship may be available for this role" | `sponsorship: yes` |
| V7 | synthetic: two samples split on sponsorship polarity | the negation gate parks the document |

The twelve v2 audit cases (C01–C12) carry over, re-emitted under schema 4.

## 8. Rulings on the review questions (Sean, 2026-10-07)

1. "Sponsorship may be available" counts as `yes`: the posting states a
   policy that can grant it.
2. F-1 CPT/OPT wording gets no field in v4. Its quote reaches the record as a
   `hiring_policy` statement.
3. The digest keeps its bound of 8 skills and reports the omitted count;
   `--full` carries every skill.

## 9. Non-goals

No concept linker (merging PyTorch and Torch is L3). No change to the
archive format, fetch cadence, or the lifecycle store. No matcher: the record
says what the posting states; judging fit stays with the agent reading it.
