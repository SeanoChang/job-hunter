"""The `demand-profile/v15` extractor prompt (parsing contract v4). Template
bytes are frozen: any edit is a PROMPT_VERSION bump (a new engine tuple),
never an in-place change.

v15 (2026-10-08) — v14 with two sections rewritten, from the v14 test run on
17 postings. (1) Authorization: citizenship is positive only when the
restriction applies to the role unconditionally. Databricks' standard
export-control paragraph ("If access to export-controlled technology ... is
required ..., Employer may decline") was read as citizenship_required, which
would hide those postings from an international student's filter; such a
conditional paragraph is now citizenship with polarity ambiguous. (2) Mentions:
the employer's social channels in a header or footer (LinkedIn, X, YouTube,
Instagram) are not mentions; the same run typed them as skills.

Every other section is v14's, reused by replacing the two sections in v14's
template; v14 stays replayable as a frozen registration.
"""

from __future__ import annotations

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2 import prompt_v12, prompt_v14
from jobhunter.l2.v2.prompt import _render, _split

PROMPT_VERSION = "demand-profile/v15"

_AUTHORIZATION_RULES = """\
WORK AUTHORIZATION: THREE FAMILIES, EACH QUOTED. facts.presence carries three
families beside experience, compensation, quantities and dates:
  sponsorship — the sentence that states a visa-sponsorship policy ("Visa will
    not sponsor applicants for work visas", "We sponsor H-1B visas",
    "Sponsorship may be available for this role").
  citizenship — the sentence that restricts eligibility by citizenship, U.S.
    person status, security clearance or export control ("Must be a U.S.
    Person due to required access to U.S. export-controlled information").
  work_authorization — a work-authorization requirement that says nothing
    about sponsorship ("Must be authorized to work in the US").
Each is {state, evidence, polarity, polarity_evidence}. state is "stated"
when the document has such a sentence, and evidence quotes it; "unresolved"
when a sentence touches the topic but you cannot tell what it says, and
evidence quotes it; "none_found" otherwise, with evidence, polarity and
polarity_evidence all null.

polarity belongs to sponsorship and citizenship, and a stated entry of either
always carries one; work_authorization's polarity and polarity_evidence are
always null. For sponsorship, positive means the employer grants it ("we
sponsor H-1B", and "sponsorship may be available" — a policy that can grant it
is positive), negative means it refuses it ("will not sponsor", "unable to
sponsor now or in the future"), ambiguous means the sentence does not settle
which. For citizenship, positive means the restriction applies to this role
unconditionally ("Must be a U.S. Person", "Active security clearance
required", "Must be a South African citizen"). A conditional or
discretionary paragraph is not that: "If access to export-controlled
technology or source code is required for performance of job duties, it is
within Employer's discretion whether to apply for a U.S. government license
... and Employer may decline to proceed with an applicant" says the
restriction applies only if some condition holds and the employer may act on
it. Record such a sentence as citizenship with polarity ambiguous, never
positive; this boilerplate sits on many postings that hire internationally.
polarity_evidence quotes the negation or modal words themselves ("will not
sponsor", "may be available", "Must be"), verbatim.

Keep the three apart. A bare "must be authorized to work in the US" is
work_authorization only: it says nothing about sponsorship, so sponsorship
stays none_found unless another sentence speaks to it. Citizenship, U.S.
person status, clearance and export control are citizenship, never
sponsorship. Never infer any of the three: no sentence, no entry —
"none_found". The sentence still becomes a statement as before (usually a
hiring_policy); the presence entry is in addition to it, never instead of it.
Code derives the reader-facing authorization summary from these entries;
never emit one.
"""

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
not mentions. Links to the employer's social channels (LinkedIn, X,
YouTube, Instagram, Facebook) in a header or footer are not mentions either.
When a phrase wraps a name, the mention is the name alone:
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

for _old in (prompt_v12._AUTHORIZATION_RULES, prompt_v14._MENTION_RULES):
    if prompt_v14.TEMPLATE.count(_old) != 1:
        raise RuntimeError("each replaced v14 section must appear exactly once in its template")

TEMPLATE = (prompt_v14.TEMPLATE
            .replace(prompt_v12._AUTHORIZATION_RULES, _AUTHORIZATION_RULES)
            .replace(prompt_v14._MENTION_RULES, _MENTION_RULES))

_PARTS = _split(TEMPLATE)


def prompt_sha() -> str:
    return sha256_hex(TEMPLATE.encode("utf-8"))


def render(markdown: str, prior_errors: list[str], prior_emit: str | None = None) -> str:
    """The v15 prompt for one attempt, through the shared v11 renderer."""
    return _render(_PARTS, markdown, prior_errors, prior_emit)
