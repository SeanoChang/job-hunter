"""Mention surfaces are copied, never rewritten.

Root cause, read from baseline train failures (`mention_ungrounded`, 34 hits in
two runs): the model expands or normalizes a name in `surface` while quoting
the shorter source text as evidence — "Expedia Group" for "Expedia",
"Maryland" for "MD", "Master's" for "Masters", "Data Center" for "centre de
données". The prompt shows surface == evidence text in every example but never
states it as a rule.
"""

ANCHOR = '"Kafka clusters" is Kafka, "Python scripts" is Python.\n'
RULE = """\
A mention's "surface" is its evidence text, character for character: copy the
name exactly as the source writes it. Never expand ("MD" stays MD, not
Maryland), complete ("Expedia" stays Expedia, not Expedia Group), correct
spelling or punctuation ("Masters" stays Masters), or translate ("centre de
données" stays centre de données). If the longer form is what you mean, quote
the longer text from the source as the evidence; if the source never writes
it, the shorter name is the mention.
"""


def transform(template: str) -> str:
    if template.count(ANCHOR) != 1:
        raise ValueError("anchor must appear exactly once")
    return template.replace(ANCHOR, ANCHOR + RULE)
