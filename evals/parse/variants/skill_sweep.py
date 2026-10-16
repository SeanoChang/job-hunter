"""skill_disciplines, plus a closing sweep over every quoted line.

The misses sit in statements the model already quoted, so the names were in
front of it when it wrote mentions. A last pass that rereads each
qualification and responsibility line against the skill test targets exactly
that gap. Measured against skill_disciplines alone to see if the pass adds
recall beyond the wider definition.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_spec = spec_from_file_location("skill_disciplines", Path(__file__).with_name(
    "skill_disciplines.py"))
assert _spec is not None and _spec.loader is not None
_base = module_from_spec(_spec)
_spec.loader.exec_module(_base)

SWEEP = """\
  Before you finish the mentions, reread every qualification and
  responsibility statement one noun phrase at a time and ask of each: is
  this a skill as defined above? Every one that is, and is not yet a
  mention, becomes one, quoted from that line.
"""


def transform(template: str) -> str:
    return _base.transform(template).replace(_base.SKILL, _base.SKILL + SWEEP)
