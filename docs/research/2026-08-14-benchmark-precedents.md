---
title: Benchmark precedents — reusable datasets and eval harnesses
date: 2026-08-14
type: report
status: current
---

# Benchmark precedents for job-hunter's eval program

Method: two research agents (skill/field extraction benchmarks; agent/
tool-interface evaluation), each followed by a citation verifier that fetched
every cited URL and spot-checked sizes, metrics, and licenses. 32 references.
Complements the layered benchmark plan discussed in conversation (2026-08-13):
this memo grounds layers 2 (extraction) and 4 (MCP/agent interface) in existing
work; layers 1, 3, and 5 rest on standard software testing, calibration/survival
methodology, and our earlier verified memos.

> [!TLDR] Both eval layers have ready-made foundations
>
> Extraction: SkillSpan (MIT) and the Decorte ESCO-linking sets (TECH/HOUSE/
> TECHWOLF, CC-BY-4.0) cover the ESCO side of the taxonomy bake-off for free;
> Lightcast has no public gold labels, so budget a small in-house set. Agent
> interface: MCPMark's task pattern (curated initial state + programmatic
> verifier, pass@1/pass^k) plus TDBench's temporal-SQL task generation are the
> two structural templates; Anthropic's tool-eval recipe supplies the metric
> list. Nothing public covers seniority/salary/remote extraction on postings —
> that gold set we build ourselves.

## Adopted benchmark program

1. **SkillSpan** (NAACL 2022; MIT repo; 14.5K sentences, 12.5K+ spans over 265
   postings; two of three subsets released) — primary span-extraction eval;
   report strict and relaxed span F1.
2. **Decorte ESCO benchmark** (TECH: 1,882 test sentences / 1,024 spans; HOUSE:
   973/786; TECHWOLF: 326 sentences / 588 sentence-level ESCO labels, CC-BY-4.0
   on Hugging Face) — the ESCO-linking eval, metrics RP@5 and MRR. TECHWOLF's
   sentence-level formulation matches LLM extractor output best.
3. **Nesta ojd_daps_skills** — reuse its dual ESCO/Lightcast mapping code and
   its model-card format; its published partial-match F1 0.590 is the
   practical-tool bar to beat.
4. **"Rethinking Skill Extraction with LLMs" harness** (NLP4HR 2024) — the
   uniformized-dataset, relaxed-matching setup for evaluating LLM extractors;
   Skill-LLM (arXiv:2410.12052) as a comparison point.
5. **Green et al. corpus** (LREC 2022; CC-BY-4.0; 18.6k entities across
   Skill/Qualification/Experience/Occupation/Domain; CRF baseline F1 0.59) — the
   only permissive set touching experience/qualification, adjacent to seniority.
6. **In-house gold set** (~300–500 postings from our own fetched corpus,
   double-annotated) — required for seniority, salary, remote, and the Lightcast
   side of the bake-off; nothing public covers these.
7. **MCPMark task/verifier pattern** (arXiv:2509.24002; MIT repo; 127 tasks;
   pass@1/pass^4; avg 16.2 turns, 17.4 tool calls) — each of our 30–50 tasks =
   frozen SQLite snapshot + natural-language prompt + Python/SQL verifier; no
   LLM judge. Its PostgreSQL track maps one-to-one onto our shape.
8. **TDBench task generation** (ICLR 2026, KAIST-led) — derive as_of/diff/
   current tasks mechanically via temporal SQL over the snapshot; adopt its
   time-accuracy metric alongside answer accuracy.
9. **Metrics from Anthropic's tool-eval guidance + τ-bench**: pass@1, pass^3,
   tool calls per task, tokens per task, tool-error rate,
   cost-per-resolved-task. τ-bench's insight: state/answer equivalence defines
   success, never trajectory match; pass^k is the reliability headline (its
   paper shows steep pass^k decay for frontier agents).
10. **Hallucination slice from BFCL v4**: 5–8 tasks whose correct answer is "not
    answerable from this store" (untracked company, date before coverage),
    scored on abstention.
11. **Ablations from Spider 2.0 / Harness / Datadog**: with vs without the ~4KB
    semantic doc (Spider 2.0 ships per-DB docs — same design); verbs-only vs
    verbs + `query_readonly` (Datadog measured ~40% cheaper runs with SQL
    capability); report tool-definition context percentage (Harness: 26% → 1.6%
    of a 200K window).
12. **Evaluator taxonomy from MCP-Universe**: format check → static ground truth
    → SQL-computed ground truth; execution-based, no LLM-as-judge.

## Extraction datasets — detail and verdicts

- **SkillSpan** — reuse as-is. Tech-posting domain, expert BIO annotation split
  into skill vs knowledge tags. One subset withheld (licensing); MIT repo.
- **Decorte TECH/HOUSE/TECHWOLF** — reuse as-is; the de-facto ESCO linking eval
  (extends SkillSpan spans with ESCO v1.1.0 labels). Their negative-sampling
  paper (RecSys-in-HR 2022) sets the RP@5/MRR convention and shows
  taxonomy-aware negatives gain up to 8 points RP@5; their 2023 follow-up
  generated 138k synthetic (sentence, skill) pairs from ESCO descriptions — the
  recipe if we ever train a local linker.
- **ESCOXLM-R** (ACL 2023) — adapt: its 9-dataset suite is the standard
  multi-dataset harness; released checkpoints are the strong non-LLM baseline.
- **Kompetencer** (Danish) and **Gnehm ICT** (German; Zenodo access restricted,
  CC-BY-NC-SA — non-commercial) — methodology-only.
- **Sayfullina 2018** (soft skills) — adapt as a soft-skill slice only.
- **Senger et al. survey** (NLP4HR 2024, arXiv:2402.05617) — the index of all
  public datasets; consult before building anything new.
- **Seniority/salary/remote** — no public posting-level benchmark exists.
  AdeptID's paper (arXiv:2501.07663; 1.2M postings, proprietary) and a 2025
  resume-seniority paper are methodology-only. Conventions: macro-F1 for
  seniority classes; exact-match with tolerance for parsed salary ranges.
- **Lightcast** — no published eval of its extractor was found; the bake-off
  needs our own labels: run both linkers on the Decorte sentences and
  hand-adjudicate disagreements, Nesta-style.

## Agent-interface benchmarks — detail and verdicts

- **MCPMark** — adapt (closest structural template; task YAML + verifier
  scripts + pass^k reporting). Best 2025 model: 52.56% pass@1, 33.86% pass^4 —
  evidence that well-built task suites still have headroom.
- **TDBench** — adapt; highest-value reuse. Auto-generates time-sensitive QA
  from temporal DBs via temporal SQL; our store is exactly its input shape.
- **MCPEval** (Salesforce) — adapt: use its auto-generation loop to draft tasks
  cheaply, then hand-verify; skip its LLM-judge scoring.
- **MCP-Bench** (arXiv preprint; venue claim unconfirmed) — methodology-only:
  phrase tasks fuzzily (the question a job-seeker would ask, not "call diff()")
  to test tool discovery.
- **MCP-Universe** (Salesforce) — methodology-only: execution-based evaluator
  taxonomy; documents token blowup with interaction depth.
- **LiveMCPBench** — methodology-only; its finding that tool-retrieval errors
  cause about half of agent failures motivates measuring whether agents pick the
  right temporal verb vs falling through to SQL.
- **τ-bench / τ²-bench** (Sierra; MIT harness) — adapt: pass^k and
  DB-state-based success. Skip the user simulator for single-shot analytical
  tasks.
- **BFCL v4** — methodology-only: the hallucination/abstention category (10% of
  its composite score) becomes our unanswerable-task slice.
- **Spider 2.0 / BIRD** — methodology-only: execution accuracy against frozen
  DBs; BIRD's follow-up analysis (execution-match agrees with human experts only
  ~62% of the time, mostly false negatives) says write equivalence-tolerant
  verifiers — order-insensitive, alias-insensitive result comparison.
- **Engineering practice** — Anthropic's "Writing effective tools for agents"
  (September 2025) is the canonical harness recipe (while-loop agent, dozens of
  multi-call tasks, metrics incl. tool errors; a 206→72 token response-format
  example). Harness and Datadog blogs supply the interface-efficiency metrics;
  Block's playbook items (names/descriptions are prompts; token-overflow guards)
  become harness assertions.

## Verifier corrections applied

- TDBench is KAIST-led (Kim, Whang, with Jindong Wang and Xing Xie), not
  "Microsoft Research" — this also corrects the attribution in our 2026-08-09
  industry memo. The 13-Allen-relations claim is body-text, not
  abstract-verifiable.
- Green et al. is 18.6k annotated entities (not ~10.6k); CRF baseline F1 0.59.
- Gnehm's Zenodo record is restricted-access, CC-BY-NC-SA (non-commercial).
- τ-bench's illustrative "90% pass@1 falls to ~57% at k=8" figure could not be
  confirmed on the abstract (paper reports under 50% pass@1 and under 25% pass^8
  for GPT-4o retail); the qualitative decay claim stands.
- MCP-Bench's NeurIPS acceptance is unconfirmed — cited as an arXiv preprint.
- BIRD SOTA is ~82% as of 2026 (the researcher's ~76% was stale).
- Anthropic post's exact title ends "— with agents."

## References

### Extraction

- [SkillSpan paper](https://aclanthology.org/2022.naacl-main.366/) — NAACL 2022;
  the anchor span dataset.
- [SkillSpan repo](https://github.com/kris927b/SkillSpan) — MIT; CoNLL+JSON; two
  subsets released.
- [Kompetencer](https://aclanthology.org/2022.lrec-1.46/) — LREC 2022; Danish;
  ESCO distant supervision recipe.
- [Gnehm ICT](https://aclanthology.org/2022.nlpcss-1.2/) — NLP+CSS 2022; German;
  fine-grained class scheme.
- [Gnehm Zenodo record](https://zenodo.org/records/6497853) — restricted access,
  CC-BY-NC-SA.
- [Green et al. paper](https://aclanthology.org/2022.lrec-1.128/) — LREC 2022;
  five entity types, 18.6k entities.
- [Green et al. data](https://github.com/acp19tag/skill-extraction-dataset) —
  CC-BY-4.0; MTurk-annotated; CRF baseline.
- [Negative sampling](https://arxiv.org/abs/2209.05987) — Decorte et al. 2022;
  RP@5/MRR convention; +8pp from ESCO negatives.
- [Gold sets](https://github.com/jensjorisdecorte/Skill-Extraction-benchmark/) —
  Decorte TECH/HOUSE/TECHWOLF benchmark repo.
- [TECHWOLF](https://huggingface.co/datasets/TechWolf/skill-extraction-techwolf)
  — 588 rows, CC-BY-4.0, sentence-level ESCO labels.
- [Synthetic pairs](https://ugentt2k.github.io/papers/2023/decorte2023ai4hr.pdf)
  — 138k synthetic pairs from ESCO descriptions.
- [ESCOXLM-R](https://aclanthology.org/2023.acl-long.662/) — ACL 2023; 9-dataset
  suite; baseline checkpoints.
- [Nesta model card](https://nestauk.github.io/ojd_daps_skills/model_card/) —
  partial F1 0.590; ESCO+Lightcast dual mapping.
- [Rethinking with LLMs](https://aclanthology.org/2024.nlp4hr-1.3/) — NLP4HR
  2024; the LLM-extractor harness.
- [Skill-LLM](https://arxiv.org/abs/2410.12052) — fine-tuned skill NER
  comparison point.
- [Survey](https://arxiv.org/abs/2402.05617) — Senger et al. 2024; dataset
  index.
- [AdeptID extraction](https://arxiv.org/abs/2501.07663) — remote/comp/
  education extraction at 1.2M scale; proprietary data.

### Agent interface

- [MCP-Bench](https://arxiv.org/abs/2508.20453) — fuzzy-instruction tool
  discovery (arXiv preprint).
- [MCPMark](https://arxiv.org/abs/2509.24002) — 127 tasks; state + verifier
  pattern; pass^4.
- [MCP-Universe](https://arxiv.org/abs/2508.14704) — execution-based evaluator
  taxonomy.
- [LiveMCPBench](https://arxiv.org/abs/2508.01780) — tool-retrieval errors cause
  ~half of failures.
- [MCPEval](https://arxiv.org/abs/2507.12806) — auto-generates tasks from an MCP
  server.
- [τ-bench](https://arxiv.org/abs/2406.12045) — pass^k; DB-state success
  criteria.
- [τ²-bench repo](https://github.com/sierra-research/tau2-bench) — MIT harness
  implementation.
- [BFCL v4](https://gorilla.cs.berkeley.edu/leaderboard.html) — hallucination/
  abstention slice precedent.
- [Spider 2.0](https://arxiv.org/abs/2411.07763) — enterprise text-to-SQL; ships
  per-DB docs.
- [BIRD](https://bird-bench.github.io/) — execution accuracy; human 92.96%, SOTA
  ~82%.
- [TDBench](https://arxiv.org/abs/2508.02045) — temporal-SQL task generation;
  time-accuracy metric (KAIST-led).
- [Tool evals](https://www.anthropic.com/engineering/writing-tools-for-agents) —
  Anthropic; canonical harness recipe and metric list.
- [Harness redesign](https://www.harness.io/blog/harness-mcp-server-redesign) —
  tool-definition context % metric.
- [Datadog](https://www.datadoghq.com/blog/engineering/mcp-server-agent-tools/)
  — SQL capability ~40% cheaper; format density.
- [Block playbook](https://engineering.block.xyz/blog/blocks-playbook-for-designing-mcp-servers)
  — names-as-prompts; overflow guards.
