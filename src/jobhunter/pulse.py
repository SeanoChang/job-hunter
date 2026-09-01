"""Digests over stored demand profiles.

`q profile` and (next) `pulse` need the same agent-facing view of one
extraction: what a posting asks for, without the evidence that proves it.
Quotes and spans stay behind `q profile --full` — a delta covering dozens of
postings must not carry the corpus text along with it.
"""

from __future__ import annotations

from typing import Any

MAX_MENTIONS = 8


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    """Areas, the top mentions across them, and the three headline facts.

    Reads defensively: `profile` is model output that passed the validator of
    its day, so a field the current schema guarantees may still be absent in a
    row written under an older one."""
    areas = (profile.get("demand_profile") or {}).get("areas") or []
    mentions: dict[str, None] = {}  # insertion-ordered set: first mention wins
    for area in areas:
        for mention in area.get("mentions") or []:
            mentions.setdefault(mention, None)
    facts = profile.get("facts") or {}
    experience = facts.get("experience_months")
    deadline = facts.get("deadline")
    return {
        "areas": [
            {"name": a.get("name"), "kind": a.get("kind"),
             "importance": a.get("importance"), "level": a.get("level")}
            for a in areas
        ],
        "mentions": list(mentions)[:MAX_MENTIONS],
        "facts": {
            "compensation": [
                {k: c.get(k) for k in ("min", "max", "currency", "period")}
                for c in facts.get("compensation") or []
            ],
            "experience_months": (
                {"min": experience.get("min"), "max": experience.get("max")}
                if experience else None
            ),
            "deadline": deadline.get("date") if deadline else None,
        },
    }
