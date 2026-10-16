"""A skill includes the established practices of every job family, not only tools.

Root cause, read from baseline misses against the 2026-10-11 answer key
(rejudged-25, 760 missed skill-samples): 661 sat in a statement the model had
already quoted, with no mention for them. They are disciplines and practices,
not tools: data analysis, inventory management, code review, root cause
analysis, supplier management, account management, preventative maintenance,
object-oriented programming. The v15 skill type names only three examples of a
discipline, all from ML and systems, so the model reads "discipline" narrowly.
"""

ANCHOR = """\
  skill — a named language, library, framework, tool, platform, protocol,
    standard or piece of hardware (Python, PyTorch, Kafka, Docker, REST, CAN,
    Pixhawk), or an established technical discipline (machine learning,
    distributed systems, computer vision)
"""
SKILL = """\
  skill — a named language, library, framework, tool, platform, protocol,
    standard, piece of hardware or equipment (Python, PyTorch, Kafka, Docker,
    REST, CAN, Pixhawk, forklift, oscilloscope), or an established discipline
    or practice of ANY job family that someone would list on a résumé or
    search a job board for: engineering (machine learning, distributed
    systems, code review, object-oriented programming, root cause analysis,
    preventative maintenance, PLC programming), data and finance (data
    analysis, statistical modeling, forecasting, financial modeling), operations
    (inventory management, supplier management, process improvement, change
    management) and sales (enterprise sales, account management, pipeline
    management). Use the shortest established name: "operator based
    preventative maintenance" is preventative maintenance. Soft skills and
    traits (communication, teamwork, leadership, ownership, attention to
    detail), spoken languages (English, Spanish) and generic nouns are never
    skills.
"""


def transform(template: str) -> str:
    if template.count(ANCHOR) != 1:
        raise ValueError("anchor must appear exactly once")
    return template.replace(ANCHOR, SKILL)
