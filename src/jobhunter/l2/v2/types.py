"""Closed enums and typed results shared by every v2 module. No runtime services.

The tuples below are the single Python-side source for the schema-2 enums; a
test asserts they match the packaged JSON so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass

STATEMENT_KINDS = ("qualification", "responsibility", "employment_constraint",
                   "compensation_statement", "hiring_policy", "employer_context")
SUBJECTS = ("candidate", "employer", "role", "unstated")
IMPORTANCE = ("required", "preferred", "not_required", "unstated", "ambiguous")
POLARITY = ("positive", "negative", "ambiguous")
PROFICIENCY = ("expert", "proficient", "working", "exposure")
OPERATORS = ("all_of", "any_of", "unresolved")
CONDITION_KINDS = ("qualification_route", "role_level", "geography",
                   "employment_type", "schedule", "other")
FAMILIES = ("experience", "compensation", "quantity", "date")
MENTION_ROLES = ("direct", "example", "contextual")
USABILITY = ("usable", "partial", "placeholder", "empty", "unsupported")
EXCLUSION_REASONS = ("eeo", "benefits", "employer_description",
                     "contact_privacy_admin", "navigation")
DISPOSITIONS = ("statements", "facts", "context", "excluded", "unresolved")
PRESENCE_STATES = ("stated", "none_found", "explicitly_absent", "unresolved")
DERIVED_STATES = ("parsed", "present_unparsed", "ambiguous", "conflicting")
COMPARISONS = ("gte", "gt", "lte", "lt", "eq", "range", "unstated")
DIMENSIONS = ("duration", "count", "percentage", "frequency")
COMPONENTS = ("base", "total", "bonus", "equity", "unspecified")
PERIODS = ("year", "month", "week", "day", "hour")
DATE_KINDS = ("application_deadline", "interview_date", "other")

# statement kinds that carry a non-null importance (spec §3: qualifications and
# employment constraints; hiring policies impose applicant rules the same way)
IMPORTANCE_KINDS = frozenset({"qualification", "employment_constraint", "hiring_policy"})


@dataclass(frozen=True)
class Block:
    """One nonempty source line under blocks/1: id, exact text, codepoint span."""

    id: str
    text: str
    span: tuple[int, int]
