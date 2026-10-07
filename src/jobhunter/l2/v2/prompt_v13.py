"""The `demand-profile/v13` extractor prompt (parsing contract v4). Template
bytes are frozen: any edit is a PROMPT_VERSION bump (a new engine tuple),
never an in-place change.

v13 (2026-10-07) — v12 with one section rewritten: the mention rules. The live
run of v12 on four entry-level postings recalled Kafka, Docker and Kubernetes
from a duty line, as v12 asked, and also typed generic nouns as skills —
Figma's "tools", "experiments", "infrastructure", "collaboration tools",
Anduril's "flight software algorithms" and "software/hardware components",
Visa's "Chatbot" and its own product "Visa Checkout". v13 says what a mention
is (a name, never a generic noun), takes the name out of a wrapping phrase
("Kafka clusters" is Kafka), and types the employer's own products as the
organization. Recall across every statement kind is unchanged.

Every other section is v12's, reused by replacing that one section in v12's
template: v12's bytes are pinned by sha in its own tests, so they cannot move
under v13, and v12 stays replayable as a frozen registration in `bundles.py`.
"""

from __future__ import annotations

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2 import prompt_v12
from jobhunter.l2.v2.prompt import _render, _split

PROMPT_VERSION = "demand-profile/v13"

_MENTION_RULES = """\
MENTIONS: RECALL EVERY NAME, THEN TYPE IT. Every named technology, tool,
language, framework, platform or method in ANY statement is a mention —
qualification lines, and equally responsibility lines, team descriptions and
example-project lists ("projects could include ..."). A duty that names Kafka
names a skill the job uses as surely as a requirement does. Link each mention
to the statement whose clause names it. Names are still never inferred from a
job title.

A mention is a NAME: a proper noun, or an established technical term someone
would put on a résumé or search a job board for. Generic nouns are not names,
whatever line they sit in: "tools", "infrastructure", "experiments",
"components", "systems", "features", "workflows", "algorithms", "models",
"simulations", "chatbot", and phrases built on them ("new collaboration
tools", "flight software algorithms", "software/hardware components") are
not mentions. When a phrase wraps a name, the mention is the name alone:
"Kafka clusters" is Kafka, "Python scripts" is Python.

Each mention carries a "type" saying what kind of thing it names:
  skill — a named language, library, framework, tool, platform, protocol,
    standard or piece of hardware (Python, PyTorch, Kafka, Docker, REST, CAN,
    Pixhawk), or an established technical discipline (machine learning,
    distributed systems, computer vision)
  field_of_study — academic fields (Computer Science, Computer Engineering)
  credential — degrees, certifications, licences (Bachelor's, CPA, AWS
    Certified)
  location — places (Toronto, San Francisco)
  organization — employers, customers, institutions, and the employer's own
    products and teams (Lyft, Visa, Visa Checkout). The employer's own product
    is never a skill: a candidate cannot bring it from elsewhere.
  other — dates, programmes, anything else (Summer 2027)
"role" stays as it was and answers a different question: how the name is used
in its sentence (direct, example, contextual). "type" says what the name is. A
city named inside a qualification line is still a location, never a skill.
"""

if prompt_v12.TEMPLATE.count(prompt_v12._MENTION_RULES) != 1:
    raise RuntimeError("v12's mention rules must appear exactly once in its template")

TEMPLATE = prompt_v12.TEMPLATE.replace(prompt_v12._MENTION_RULES, _MENTION_RULES)

_PARTS = _split(TEMPLATE)


def prompt_sha() -> str:
    return sha256_hex(TEMPLATE.encode("utf-8"))


def render(markdown: str, prior_errors: list[str], prior_emit: str | None = None) -> str:
    """The v13 prompt for one attempt, through the shared v11 renderer, so a
    v13 retry renders exactly like a v12 one."""
    return _render(_PARTS, markdown, prior_errors, prior_emit)
