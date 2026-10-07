"""The validator identifier itself: it bumps, and its history never rewrites.

`VALIDATOR_VERSION` freezes `facts.py`'s derivation grammars together with
`v2/verify.py`'s check table, so every archived record is judged by the rules
its identifier names. The grammar cases live in `test_facts_quantity.py` and
`test_facts_money_date.py`; this module pins the identity.
"""

from __future__ import annotations

import inspect

from jobhunter.l2.v2 import facts
from jobhunter.l2.v2.facts import VALIDATOR_VERSION


def test_validator_version_is_20() -> None:
    assert VALIDATOR_VERSION == "20"


def test_every_validator_identifier_keeps_its_history_line() -> None:
    """A bumped identifier adds a line; it never edits one. 13 is the first v2
    identifier (11/12 were v1's), 20 is this contract's."""
    source = inspect.getsource(facts)
    missing = [f"validator/{n}" for n in range(13, 21) if f"validator/{n}" not in source]
    assert missing == []


def test_the_in_place_amendment_of_20_is_recorded() -> None:
    """Validator 20 was amended in place (2026-09-28) instead of bumped, which
    is only legitimate because 20 had not gone live on main. The history says
    so, and says what moved, or a reader of an archived stamp cannot tell which
    20 judged it."""
    source = inspect.getsource(facts)
    assert "validator/20, amended in place (2026-09-28" in source
    for moved in ("the unit is the word the number carries",
                  "format", "topic"):
        assert moved in source.lower(), moved
