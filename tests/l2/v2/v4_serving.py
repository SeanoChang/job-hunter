"""Settled schema-4 records for the read-surface tests (parsing contract v4 §5).

Each one is a §7 regression fixture from `conftest` (V1-V6), assembled under
schema 4 and given the settlement overlay `runner.settle` attaches, so the
store and the read surface see exactly what the runner would hand them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tests.l2.v2.conftest import (
    ANDURIL_MD,
    FIGMA_MD,
    LYFT_MD,
    MAY_SPONSOR_MD,
    VISA_MD,
    WORK_AUTH_MD,
    _settled,
    assemble4,
    make_anduril_emit,
    make_figma_emit,
    make_lyft_emit,
    make_may_sponsor_emit,
    make_visa_emit,
    make_work_auth_emit,
)

#: name -> (emit factory, markdown): V1 visa, V2 lyft, V3 figma, V4 anduril,
#: V5 work_auth, V6 may_sponsor
CASES: dict[str, tuple[Callable[[], dict[str, Any]], str]] = {
    "visa": (make_visa_emit, VISA_MD),
    "lyft": (make_lyft_emit, LYFT_MD),
    "figma": (make_figma_emit, FIGMA_MD),
    "anduril": (make_anduril_emit, ANDURIL_MD),
    "work_auth": (make_work_auth_emit, WORK_AUTH_MD),
    "may_sponsor": (make_may_sponsor_emit, MAY_SPONSOR_MD),
}


def v4_record(name: str, *, lifecycle: str = "needs_review") -> dict[str, Any]:
    """One §7 case as a settled schema-4 record."""
    emit, markdown = CASES[name]
    return _settled(assemble4(emit(), markdown), lifecycle)
