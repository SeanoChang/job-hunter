"""Claim-level projection: one row per (mention, statement) link (spec §3).

This is the C04/C12 fix in code form. A projected row carries the
*statement's* own importance and polarity — never an area label (the
production defect this plan exists to kill: `store/extraction.py`'s
`upsert_state` writes `area["importance"]` into `profile_mentions`, so a
preferred certification sitting next to a required degree in one
presentation area reads back as required). Mentions are atomic by contract;
this module never re-splits one.
"""

from __future__ import annotations

from typing import Any


def _group_ids_by_statement(record: dict[str, Any]) -> dict[str, list[str]]:
    """Direct membership only: every group a statement is listed under."""
    index: dict[str, list[str]] = {}
    for group in record["relations"]["groups"]:
        for member in group["members"]:
            index.setdefault(member, []).append(group["id"])
    return index


def mention_rows(record: dict[str, Any], include_ineligible: bool = False) -> list[dict[str, Any]]:
    """One row per (mention, statement) pair, in mention then statement order.

    Rows are emitted only when `record["quality"]["search_eligible"]` is True
    unless `include_ineligible=True` is passed (the inspection surface, spec
    §7) — the store wires the default path in increment 3.
    """
    if not include_ineligible and not record["quality"]["search_eligible"]:
        return []
    statements = {s["id"]: s for s in record["statements"]}
    group_ids_by_statement = _group_ids_by_statement(record)
    rows: list[dict[str, Any]] = []
    for mention in record["mentions"]:
        for statement_id in mention["statement_ids"]:
            statement = statements[statement_id]
            rows.append({
                "mention_id": mention["id"],
                "statement_id": statement_id,
                "surface": mention["surface"],
                "normalized_key": mention["normalized_key"],
                "role": mention["role"],
                "kind": statement["kind"],
                "subject": statement["subject"],
                "importance": statement["importance"],
                "polarity": statement["polarity"],
                "condition_ids": list(statement["condition_ids"]),
                "group_ids": list(group_ids_by_statement.get(statement_id, [])),
            })
    return rows
