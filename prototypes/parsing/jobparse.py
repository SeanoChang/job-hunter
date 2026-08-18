#!/usr/bin/env python3
"""Job posting -> document nodes -> requirement EXPRESSIONS (tier-1, rules only).

Merges the earlier prototypes:
  * parse_job.py      HTML -> blocks -> nodes with section context, demand ladder,
                      YOE, importance override
  * parse_req_expr.py bullet -> {op, atoms[]} with typed atoms, exemplars, open_class

Contract (see gold/README.md): every requirement bullet becomes ONE expression.
Rules never guess syntax they cannot see: comma-scope inside noun phrases,
contrastive clauses, and bullets with no recognizable atom are FLAGGED
(`needs_tier2`) for the LLM re-treeing pass instead of being silently dropped
or shredded. Every atom carries a span into the normalized document text.
Stdlib only.
"""
import hashlib
import html as htmllib
import json
import re
import unicodedata
from html.parser import HTMLParser

# ----------------------------------------------------------------- sections
SECTION_RULES = [
    (r"about (the )?(role|this role|position)|the role\b", "role_summary"),
    (r"about (us|the (company|team))|who we are|our (mission|story)|about \w+$", "about_company"),
    (r"what you('|’)ll (do|achieve|be doing)|responsibilities|in this role|the role|day.to.day|^role$",
     "responsibilities"),
    (r"what (we('|’)re looking for|we need to see|you('|’)ll (need|bring))|skills you('|’)ll need|"
     r"requirements|qualifications|you (may|might) be a good fit|about you|must have",
     "requirements_required"),
    (r"nice to have|preferred|bonus|strong candidates (may|will)|plus(es)?\b|ways to stand out",
     "requirements_preferred"),
    (r"benefits|perks|what we offer|compensation|salary|pay range", "benefits"),
    (r"equal (employment )?opportunity|eeo|accommodation|diversity statement|"
     r"how we('|’)re different|privacy", "boilerplate"),
    (r"interview process|hiring process|what to expect", "process"),
    (r"logistics|eligibility|location|visa|education requirement", "eligibility"),
]
REQUIREMENT_SECTIONS = {"requirements_required": "required",
                        "requirements_preferred": "preferred"}

# ----------------------------------------------------------------- concepts
# alias table v0: (regex, concept_id, kind). Matched longest-alias-first with
# non-overlapping spans, so "LLM training" wins over "LLM", "front-end" over
# nothing, etc. Case-insensitive unless the alias is a short case-sensitive
# token (Go, R). This is the seed of the `concepts` table.
_ALIASES = [
    (r"python", "skill:python", "language"),
    (r"Go(?=\b)(?!\s+(?:the|to|for|beyond|live|and))|golang", "skill:go", "language"),
    (r"rust", "skill:rust", "language"),
    (r"typescript", "skill:typescript", "language"),
    (r"javascript", "skill:javascript", "language"),
    (r"react", "skill:react", "framework"),
    (r"kubernetes|k8s", "skill:kubernetes", "platform"),
    (r"docker", "skill:docker", "platform"),
    (r"terraform", "skill:terraform", "tool"),
    (r"aws", "skill:aws", "platform"),
    (r"gcp|google cloud", "skill:gcp", "platform"),
    (r"postgres(?:ql)?", "skill:postgres", "platform"),
    (r"ci/cd(?: pipelines?)?", "skill:cicd", "practice"),
    (r"distributed systems?", "skill:distributed-systems", "domain"),
    (r"machine learning|\bml\b", "skill:ml", "domain"),
    (r"llm training", "skill:llm-training", "domain"),
    (r"fine[- ]tuning", "skill:llm-finetuning", "domain"),
    (r"evaluation workflows?|\bevals?\b", "skill:llm-eval", "domain"),
    (r"llms?|large language models?", "skill:llm", "domain"),
    (r"trio", "skill:trio", "framework"),
    (r"asyncio", "skill:asyncio", "framework"),
    (r"api design", "skill:api-design", "practice"),
    (r"dashboards?", "skill:dashboards", "domain"),
    (r"monitoring", "skill:monitoring", "domain"),
    (r"observability(?: tooling)?", "skill:observability", "domain"),
]
_CASE_SENSITIVE = {"skill:go"}
ALIASES = sorted(_ALIASES, key=lambda a: -len(a[0]))
CONCEPT_KIND = {cid: kind for _, cid, kind in _ALIASES}

# umbrella head phrases -> family / abstract atoms
UMBRELLAS = [
    (r"cloud infrastructure", "family", "family:cloud-infra"),
    (r"modern web stack|frontend stack|front.end stack", "family", "family:web-frontend"),
    (r"frontend engineering|front.end engineering", "family", "family:web-frontend"),
    (r"async python", "family", "family:python-async"),
    (r"software engineering fundamentals", "abstract", "abstract:swe-fundamentals"),
    (r"full.stack range|full.stack", "abstract", "abstract:fullstack"),
    (r"distributed systems", "family", "family:distributed-systems"),
]

TRAIT = re.compile(
    r"^(?:care about|communicate|operate with|thrive|desire to|alignment with|"
    r"knack for|have found yourself|are comfortable|ability to|passion(?:ate)? for|"
    r"comfortable with|willing(?:ness)? to|excited (?:about|by)|self.starter|"
    r"strong communicat|attention to detail)", re.I)
CAPABILITY = re.compile(
    r"\b(?:built|build(?:ing)?|shipping|shipped|ship\b|track record|"
    r"background (?:building|in)|experience (?:working|building|designing|leading)|"
    r"develop(?:ing|ed)? projects|designed|led\b|owned)\b", re.I)
CONTRAST = re.compile(r"\bnot just\b|\bnot only\b|\brather than\b|\binstead of\b", re.I)
OPEN_CLASS = re.compile(r"or similar|or equivalent|\betc\b|e\.g\.|ideally|such as", re.I)
DEGREE = re.compile(r"\b(?:bachelor|master|phd|doctorate|b\.?s\.?c?|m\.?s\.?c?)('s)?\b(?=.*\b(degree|in)\b)", re.I)
YOE = re.compile(r"(\d+)\s*\+?\s*(?:years?|yrs?)", re.I)
DEMAND_LEVELS = [  # first match wins
    (r"\bexpert\b|deep (understanding|knowledge|experience)|mastery", "expert"),
    (r"\bproficien(t|cy)\b|\bfluen(t|cy)\b|\bstrong\b.*\b(skills?|fundamentals|background)\b", "proficient"),
    (r"\bfamiliar(ity)?\b|\bexposure to\b|\bawareness\b|\bbasic\b", "exposure"),
    (r"\bexperience (with|in|building|working)\b|\btrack record\b|\bbackground (in|building)\b|\bability to\b", "working"),
]
SPLIT = re.compile(r",\s*(?:and\s+|or\s+)?|\s+\band\b\s+|\s+\bor\b\s+", re.I)
CORRELATIVE = re.compile(r"\b(?:both|between)\b[^,]*?\b(and)\b", re.I)


# -------------------------------------------------------- HTML -> node stream
class Walker(HTMLParser):
    """Flatten HTML into (kind, text) blocks: heading | bullet | para."""
    HEADINGS = {"h1", "h2", "h3", "h4", "h5"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self._buf, self._kind, self._stack = [], [], "para", []
        self._li_depth, self._strong_only = 0, True

    def _flush(self):
        text = unicodedata.normalize("NFKC", " ".join("".join(self._buf).split()))
        if text:
            kind = self._kind
            if kind == "para" and self._li_depth > 0:
                kind = "bullet"          # <li><p>text</p></li> stays a bullet
            if kind == "para" and self._strong_only and len(text) < 80:
                kind = "heading"         # <p><strong>Requirements</strong></p>
            self.blocks.append((kind, text))
        self._buf, self._kind, self._strong_only = [], "para", True

    def handle_starttag(self, tag, attrs):
        if tag in self.HEADINGS:
            self._flush(); self._kind = "heading"
        elif tag == "li":
            self._flush(); self._kind = "bullet"; self._li_depth += 1
        elif tag in ("p", "div", "ul", "ol", "br"):
            self._flush()
        self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.HEADINGS or tag in ("li", "p", "div"):
            self._flush()
        if tag == "li" and self._li_depth > 0:
            self._li_depth -= 1
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data):
        if data.strip() and not any(t in ("strong", "b") for t in self._stack):
            if self._kind == "para":
                self._strong_only = False
        self._buf.append(data)


def classify_heading(text):
    t = text.lower().strip().rstrip(":")
    for pat, s in SECTION_RULES:
        if re.search(pat, t):
            return s
    return None


def build_nodes(html_doc):
    """HTML -> (normalized text, nodes with spans + section context)."""
    w = Walker(); w.feed(html_doc); w._flush()
    parts, nodes, cursor, section = [], [], 0, "other"
    for i, (kind, btext) in enumerate(w.blocks):
        if kind == "heading":
            section = classify_heading(btext) or "other"   # unmapped heading RESETS
        start = cursor
        parts.append(btext)
        cursor += len(btext) + 1
        nodes.append({"idx": i, "kind": kind, "section": section,
                      "heading_raw": btext if kind == "heading" else None,
                      "start": start, "end": start + len(btext),
                      "node_hash": hashlib.sha256(btext.encode()).hexdigest()[:12]})
    return "\n".join(parts), nodes


# ------------------------------------------------------------ concept hits
def find_concepts(text, base=0):
    """Non-overlapping alias hits, longest MATCHED TEXT first. -> [(cid, start, end)]
    (Sorting by pattern length is wrong: `llms?|large language models?` is a long
    pattern that matches a short word and would shadow `llm training`.)"""
    cands = []
    for pat, cid, _kind in ALIASES:
        flags = 0 if cid in _CASE_SENSITIVE else re.I
        for m in re.finditer(r"(?<![\w-])(?:" + pat + r")(?![\w-])", text, flags):
            cands.append((m.end() - m.start(), m.start(), m.end(), cid))
    taken, hits = [], []
    for _len, s, e, cid in sorted(cands, key=lambda c: (-c[0], c[1])):
        if any(s < te and e > ts for ts, te in taken):
            continue
        taken.append((s, e))
        hits.append((cid, base + s, base + e))
    return sorted(hits, key=lambda h: h[1])


# ---------------------------------------------------- bullet -> expression
def _mask(seg):
    """Same-length masking: parenthetical content -> \\x00, correlative 'and' -> \\x01.
    Offsets stay identical to the original bullet. Returns (masked, paren_groups)."""
    masked = list(seg)
    groups = []
    for m in re.finditer(r"\(([^)]*)\)", seg):
        groups.append({"start": m.start(), "end": m.end(), "inner": m.group(1)})
        for i in range(m.start(), m.end()):
            masked[i] = "\x00"
    for m in CORRELATIVE.finditer("".join(masked)):
        for i in range(m.start(1), m.end(1)):
            masked[i] = "\x01"
    return "".join(masked), groups


def _chunks(masked):
    """Top-level coordination split on the masked text -> [(start, end)]."""
    out, last = [], 0
    for m in SPLIT.finditer(masked):
        out.append((last, m.start())); last = m.end()
    out.append((last, len(masked)))
    return [(s, e) for s, e in out if masked[s:e].strip(" \x00\x01,.;")]


def _classify(seg, span, groups):
    """One top-level chunk -> atom dict (kind, target, text, span, ...)."""
    s, e = span
    inner = [g for g in groups if g["start"] >= s and g["end"] <= e]
    head = seg[s:e]
    for g in inner:                       # drop parentheticals from the head text
        head = head.replace(seg[g["start"]:g["end"]], " ")
    head = " ".join(head.split()).strip(" ,.;—-")
    exemplars, open_class = [], False
    for g in inner:
        exemplars += [cid for cid, _, _ in find_concepts(g["inner"])]
        open_class |= bool(OPEN_CLASS.search(g["inner"]))
    hl = head.lower()
    for pat, kind, cid in UMBRELLAS:
        m = re.search(pat, hl)
        if m:
            return {"kind": kind, "target": cid, "text": head, "span": [s, e],
                    "exemplars": exemplars, "open_class": open_class}
    hits = find_concepts(head)
    if len(hits) == 1 and not exemplars:
        cid, hs, he = hits[0]
        return {"kind": "skill", "target": cid, "text": head, "span": [s, e],
                "concept_kind": CONCEPT_KIND.get(cid)}
    if TRAIT.match(hl):
        return {"kind": "trait", "target": None, "text": head, "span": [s, e]}
    if CAPABILITY.search(hl):
        return {"kind": "capability", "target": None, "text": head, "span": [s, e],
                "concepts_nearby": [h[0] for h in hits]}
    if hits:                              # several concepts, no umbrella head
        return {"kind": "skill*", "target": [h[0] for h in hits], "text": head, "span": [s, e]}
    return {"kind": "other", "target": None, "text": head, "span": [s, e]}


def parse_bullet(seg):
    """Requirement bullet text -> expression + tier-2 routing flags."""
    masked, groups = _mask(seg)
    demand = next((lvl for pat, lvl in DEMAND_LEVELS if re.search(pat, seg.lower())), "working")
    yoe = YOE.search(seg)
    atoms_all = [_classify(seg, sp, groups) for sp in _chunks(masked)]
    atoms = [a for a in atoms_all if a["kind"] != "other"]
    residue = [a["text"] for a in atoms_all if a["kind"] == "other"]

    has_or = bool(re.search(r"\s\bor\b\s", masked, re.I))
    has_and = bool(re.search(r"\s\band\b\s", masked, re.I))
    if len(atoms) <= 1:
        op = "SINGLE"
    elif has_or and has_and:
        op = "MIXED"
    elif has_or:
        op = "OR"
    else:
        op = "AND"                        # explicit "and" or asyndetic comma list
    if len(atoms) == 1 and atoms[0]["kind"] in ("trait", "capability"):
        atoms[0]["text"] = seg.strip()    # residue is elaboration of the one atom
        atoms[0]["span"] = [0, len(seg)]

    reasons = []
    if not atoms:
        reasons.append("no_atoms")
    if op == "MIXED":
        reasons.append("mixed")
    if residue and "," in masked:
        reasons.append("comma_residue")
    if any(a["kind"] == "skill*" for a in atoms):
        reasons.append("unresolved")
    if CONTRAST.search(seg):
        reasons.append("contrast")
    if DEGREE.search(seg):
        atoms.append({"kind": "credential", "target": "credential:degree",
                      "text": DEGREE.search(seg).group(0), "span": list(DEGREE.search(seg).span())})
    return {"expr": {"op": op, "atoms": atoms}, "demand": demand,
            "min_months": int(yoe.group(1)) * 12 if yoe else None,
            "residue": residue, "needs_tier2": bool(reasons), "tier2_reasons": reasons}


def extract_requirements(text, nodes):
    reqs = []
    for n in nodes:
        if n["kind"] != "bullet" or n["section"] not in REQUIREMENT_SECTIONS:
            continue
        seg = text[n["start"]:n["end"]]
        importance = REQUIREMENT_SECTIONS[n["section"]]
        if importance == "required" and re.search(r"\bpreferred\b|nice to have", seg.lower()):
            importance = "preferred"      # bullet wording beats section context
        r = parse_bullet(seg)
        for a in r["expr"]["atoms"]:      # atom spans -> document coordinates
            a["span"] = [n["start"] + a["span"][0], n["start"] + a["span"][1]]
        r.update({"node": n["idx"], "section": n["section"], "text": seg,
                  "importance": importance, "assertion": "inferred",
                  "provenance": {"node": n["idx"], "span": [n["start"], n["end"]],
                                 "node_hash": n["node_hash"], "section": n["section"]}})
        reqs.append(r)
    return reqs


def flatten(expr):
    """Legacy keyword projection: concept ids mentioned anywhere in the expression."""
    out = []
    for a in expr["atoms"]:
        if a["kind"] in ("skill", "family", "abstract", "credential"):
            out.append(a["target"])
        out += a.get("exemplars", [])
        if a["kind"] == "skill*":
            out += a["target"]
    return out


# ------------------------------------------------------------- entry points
def parse_html(source, posting_id, title, html_doc):
    text, nodes = build_nodes(html_doc)
    return {"source": source, "id": str(posting_id), "title": title, "text": text,
            "nodes": nodes, "requirements": extract_requirements(text, nodes)}


def parse_fixture(path):
    job = json.load(open(path))
    if job.get("source"):                                               # explicit (e.g. pasted text fixtures)
        return parse_html(job["source"], job["id"], job["title"], job["descriptionHtml"])
    if "content" in job and "descriptionHtml" not in job:            # Greenhouse
        return parse_html("greenhouse", job["id"], job["title"], htmllib.unescape(job["content"]))
    if "descriptionHtml" in job:                                        # Ashby
        return parse_html("ashby", job["id"], job["title"], job["descriptionHtml"])
    raise ValueError(f"unrecognized payload shape: {path}")


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        doc = parse_fixture(p)
        print(f"== {doc['source']} {doc['id']}: {doc['title']} — {len(doc['nodes'])} nodes, "
              f"{len(doc['requirements'])} requirement bullets")
        for r in doc["requirements"]:
            flag = f"  <- tier2:{','.join(r['tier2_reasons'])}" if r["needs_tier2"] else ""
            print(f"  n{r['node']:<3} {r['importance']:<9} {r['demand']:<10} op={r['expr']['op']:<6}"
                  f" {[a['target'] or a['kind'] for a in r['expr']['atoms']]}{flag}")
