# Gold set — requirement bullets as expression trees

Hand-labeled 2026-08-17 from the two fixture postings (Anthropic Greenhouse
5186067008, Ramp Ashby 4e64ab86). One JSON object per line in
`requirement_bullets.jsonl`.

## Record shape

```jsonc
{
  "id": "gh:5186067008:n19", // source:posting:node
  "source": "greenhouse",
  "node": 19, // node idx from the document-node stage
  "text": "Are proficient in Python and a modern web stack (React, TypeScript, or similar)",
  "importance": "required", // required | preferred  (after wording override)
  "demand": "proficient", // expert | proficient | working | exposure
  "min_months": null,
  "tier": 1, // 1 = rules must produce the exact tree
  // 2 = rules must FLAG (needs_tier2); judge produces the tree
  "expr": {
    "op": "AND", // SINGLE | AND | OR | MIXED
    "atoms": [
      { "kind": "skill", "target": "skill:python" },
      {
        "kind": "family",
        "target": "family:web-frontend",
        "exemplars": ["skill:react", "skill:typescript"],
        "open_class": true,
      },
    ],
  },
}
```

## Atom kinds

| kind         | meaning                                            | compared on             |
| ------------ | -------------------------------------------------- | ----------------------- |
| `skill`      | nameable registry concept (language/framework/…)   | kind + target           |
| `family`     | umbrella with members/facets + verbatim exemplars  | + exemplars, open_class |
| `abstract`   | decomposable competence ("swe fundamentals")       | kind + target           |
| `capability` | demonstrated act/artifact ("built X", "shipped Y") | kind + count            |
| `trait`      | disposition / soft skill                           | kind + count            |
| `credential` | degree / certification                             | kind + target           |

`other` atoms emitted by the parser (unclassified residue) are ignored by the
comparison and reported separately as residue.

## Conventions

- Truth is labeled the same regardless of tier; `tier` only says who is expected
  to reach it.
- A bullet whose whole content is one disposition/act is ONE atom even if it
  contains commas or "and" ("Desire to work in a fast-paced environment,
  continuously grow, and master your craft" → one trait).
- Exemplars inside a parenthetical are inherently "one of these"; `open_class`
  is true when the text says "or similar / etc. / e.g. / ideally".
- Comma lists joined by "or" are OR; asyndetic comma lists are AND; both
  conjunctions at top level are MIXED.
- `demand` follows the wording ladder; default `working`. Trait-only bullets
  keep the default — demand is not meaningful for them and is not assessed.
- `importance` is after the bullet-level wording override (Ramp n18: "…
  preferred" inside the required section → preferred).
