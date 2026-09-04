<!-- pmi:generated - rebuilt by `pmi init --regen`, by /please-map-it:setup,
and at every boundary close. A file without this marker line is copied to
home.md.local before it is replaced; once this line is here, edits are not
preserved - delete it first to keep them. -->

# please-map-it home


The entry point for this repo's work records. Ladder: this page → a question
subtree or canvas → a single record → its provenance line.

- [[please-map-it/web|Thinking web]] · [[please-map-it/log|Session log]] ·
  views in `please-map-it/views/`

- Conventions: mapped with defaults, interview pending

## Pending (needs triage)

```dataview
TABLE type, status FROM "please-map-it/web" WHERE status = "proposed" OR status = "proto"
```

## Holes

Run `pmi web holes` — questions without options, decisions without evidence.

## Board

[[views/board.base|Ready board]] · [[views/milestones.base|Milestones]]

Run `pmi graph canvas <plan>` for one plan's execution DAG.

## How to read please-map-it records

Five node types: question, option, argument (signed), decision, evidence.
Statuses: proposed (agent-written, unreviewed) → accepted (you promoted it
via /please-map-it:triage) · superseded/rejected (excluded from every
render). Edges are the `field:: [[link]]` lines; backlinks show what fed a
decision. Protonodes in `please-map-it/web/inbox/` are verbatim strays
awaiting typing.
