"""Mentions are emitted statement by statement, from each quote, with a matched precision guard.

Hypothesis: 661 of 760 missed skill-samples sat in a statement the model had
already quoted. The miss is a pass-ordering problem, not a definition problem:
the model collects names in a separate pass and skips practice phrases inside
quotes it already wrote. Tying a per-quote checklist to each qualification and
responsibility statement should lift recall; a paired list of frequent false
skills (bare "AI", spoken languages, employer products) keeps precision level.
"""

ANCHOR = (
    "to the statement whose clause names it. Names are still never inferred from a\n"
    "job title.\n"
)
RULE = """\

WORK QUOTE BY QUOTE. Do not collect mentions in a separate pass. Each time you
write a qualification, responsibility or team statement, reread its quote
before you go on to the next statement, and emit one mention for every name
in that quote. A practice phrase is a name too: "experience with data analysis
and forecasting" gives data analysis and forecasting; "perform code reviews
and root cause analysis" gives code review and root cause analysis; "operate a
pallet jack or hand truck" gives pallet jack and hand truck. Inventory
management, supplier management, change management, software architecture,
design patterns, statistical modeling, object-oriented programming, supply chain management,
backend development and source control are skills of this kind; so are
robotics and data science when the line asks for them as working
practice rather than as a degree subject. A quoted line that names such a
practice and has no mention linked to it is incomplete.

The reread also removes words that are not names. Never emit these as skills:
a bare umbrella word ("AI", "IT", "cloud", "network", "internet", "design",
"deployment", "SLA"), a spoken language (English, Spanish), a regulation or
compliance regime used as a hiring condition (ITAR), an IP address or version
number, a game or consumer brand the employer makes, or the employer's own
internal product (DashPass).
"""


def transform(template: str) -> str:
    if template.count(ANCHOR) != 1:
        raise ValueError("anchor must appear exactly once")
    return template.replace(ANCHOR, ANCHOR + RULE)
