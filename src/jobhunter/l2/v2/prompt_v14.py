"""The `demand-profile/v14` extractor prompt (parsing contract v4). Template
bytes are frozen: any edit is a PROMPT_VERSION bump (a new engine tuple),
never an in-place change.

v14 (2026-10-08) — v13 with a narrower own-product rule. v13 typed the
employer's own product as an organization on the reasoning that a candidate
cannot bring it from elsewhere. Over the 1,219-posting entry-level run that put
AWS under organization in 145 Amazon postings, Figma in 72 Figma postings,
Databricks in 21 and Snowflake in 7: public platforms anyone can bring, so a
skill filter for AWS missed Amazon's own jobs. v14 keeps only internal products
(Visa Checkout) as organizations, keeps a sold platform a skill even in its
maker's posting, and says a tool that shares a company's name is a skill when
the line uses it as a tool (GitHub at Pinterest read as an organization).

Every other section is v13's (and so v12's), reused by replacing the one
section in v13's template; v13 stays replayable as a frozen registration.
"""

from __future__ import annotations

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2 import prompt_v13
from jobhunter.l2.v2.prompt import _render, _split

PROMPT_VERSION = "demand-profile/v14"

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
  organization — employers, customers and institutions (Lyft, Visa), and the
    employer's internal products and features that exist only inside it
    (Visa Checkout)
  other — dates, programmes, anything else (Summer 2027)
A product or platform the employer sells to other companies stays a skill,
even in its maker's own posting: AWS in an Amazon posting, Databricks in a
Databricks posting, Figma in a Figma posting, Snowflake in a Snowflake
posting. A tool that shares its maker's name (GitHub, Salesforce) is a skill
when the line uses it as a tool, and an organization only when the line names
the company.
"role" stays as it was and answers a different question: how the name is used
in its sentence (direct, example, contextual). "type" says what the name is. A
city named inside a qualification line is still a location, never a skill.
"""

if prompt_v13.TEMPLATE.count(prompt_v13._MENTION_RULES) != 1:
    raise RuntimeError("v13's mention rules must appear exactly once in its template")

TEMPLATE = prompt_v13.TEMPLATE.replace(prompt_v13._MENTION_RULES, _MENTION_RULES)

_PARTS = _split(TEMPLATE)


def prompt_sha() -> str:
    return sha256_hex(TEMPLATE.encode("utf-8"))


def render(markdown: str, prior_errors: list[str], prior_emit: str | None = None) -> str:
    """The v14 prompt for one attempt, through the shared v11 renderer."""
    return _render(_PARTS, markdown, prior_errors, prior_emit)
