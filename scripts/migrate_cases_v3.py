"""Regenerate the shared case corpus into schema-3 emits (plan Task 6).

The twelve case fixtures are the corpus every offline contract and every
scripted runner ladder is driven from, and the v20 bump moved the active
bundle to `(demand-profile/v11, 3, 20)`. Hand-editing eleven emits into the new
shape would put the fixtures and the migration rule on two different rules —
the drift this script exists to make impossible: the schema-3 emits ARE
`migrate.emit3_of` applied to the schema-2 ones, and a test re-derives them to
prove it.

Layout, after this has run once:

    cases/<case>.source.md    the excerpt (shared; never rewritten)
    cases/<case>.emit2.json   the FROZEN schema-2 emit — the source of truth
    cases/<case>.emit.json    the derived schema-3 emit — the active shape

`.emit2.json` is written once, from whatever `.emit.json` held at the time, and
is never derived from anything afterwards: it is a shipped corpus partition
(`get_bundle_for_tuple("demand-profile/v10", "2")` still judges it, and
`test_rebuild` still replays it). Re-running the script therefore re-derives
`.emit.json` from `.emit2.json` and is idempotent.

Usage:
    uv run python scripts/migrate_cases_v3.py          # rewrite the fixtures
    uv run python scripts/migrate_cases_v3.py --check  # verify, write nothing
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from jobhunter.l2.schemas import validate_emit
from jobhunter.l2.v2.migrate import emit3_of
from jobhunter.l2.v2.source import annotate

CASES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "l2" / "v2" / "cases"


def _read(path: pathlib.Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _dump(fixture: dict[str, Any]) -> str:
    """The corpus's own formatting: two-space indent, real UTF-8, one newline."""
    return json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"


def derived_fixture(fixture: dict[str, Any], markdown: str) -> dict[str, Any]:
    """The schema-3 fixture for one case: same wrapper, derived emit.

    The annotation is passed so a whole-block `importance_evidence` reference
    can be read for a modal term — without it every such quote would derive
    null and the corpus would lose modality the archive actually holds.
    """
    return {**fixture, "emit": emit3_of(fixture["emit"], blocks=annotate(markdown))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if a fixture is not what the derivation produces")
    args = parser.parse_args(argv)

    stale: list[str] = []
    for active in sorted(CASES.glob("*.emit.json")):
        case = active.name.removesuffix(".emit.json")
        frozen = CASES / f"{case}.emit2.json"
        if not frozen.exists():
            if args.check:
                print(f"{case}: no frozen schema-2 emit beside the derived one")
                stale.append(case)
                continue
            # The freeze seeds the frozen partition ONCE, from the schema-2 emit
            # `.emit.json` held before the bump. It holds the derivation now, so
            # a blind copy would make schema 3 the "schema-2" source — and the
            # next derivation would read no `importance_evidence`, wipe the
            # case's modality, and still write a schema-3-valid fixture, with
            # `--check` satisfied because both sides moved together. The frozen
            # bytes are an invariant: report, never re-seed from the derivation.
            if problems := validate_emit(_read(active)["emit"], "2"):
                print(f"{case}: {active.name} is not a schema-2 emit to freeze: {problems[0]}")
                stale.append(case)
                continue
            frozen.write_text(active.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"{case}: froze the schema-2 emit as {frozen.name}")
        source = _read(frozen)
        # Everything downstream is defined by these bytes, so they are checked
        # on every run, `--check` included: a damaged frozen partition would
        # otherwise redefine the corpus and be rewritten into agreement with
        # itself rather than reported.
        if problems := validate_emit(source["emit"], "2"):
            print(f"{case}: {frozen.name} is not a valid schema-2 emit: {problems[0]}")
            stale.append(case)
            continue
        markdown = (CASES / f"{case}.source.md").read_text(encoding="utf-8")
        fixture = derived_fixture(source, markdown)
        if problems := validate_emit(fixture["emit"], "3"):
            print(f"{case}: derived emit is not schema-3 valid: {problems[0]}")
            stale.append(case)
            continue
        text = _dump(fixture)
        if active.read_text(encoding="utf-8") == text:
            continue
        if args.check:
            print(f"{case}: {active.name} is not what migrate.emit3_of derives")
            stale.append(case)
            continue
        active.write_text(text, encoding="utf-8")
        print(f"{case}: rewrote {active.name} as a schema-3 emit")
    if stale:
        print(f"{len(stale)} case(s) out of date: {', '.join(stale)}")
        return 1
    print("every case fixture is the derivation of its frozen schema-2 emit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
