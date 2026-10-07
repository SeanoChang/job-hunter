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

# schema 4 (parsing contract v4 §2.1–2.5)
MENTION_TYPES = ("skill", "field_of_study", "credential", "location", "organization", "other")
TRACK_SELECTIONS = ("candidate_choice", "team_match", "unstated")
#: the three authorization presence families, in schema order
AUTHORIZATION_FAMILIES = ("sponsorship", "citizenship", "work_authorization")
#: the families whose `polarity` means something; work_authorization's is null
POLARIZED_AUTHORIZATION = ("sponsorship", "citizenship")
AUTHORIZATION_STATES = ("stated", "none_found", "unresolved")
#: the code-derived `authorization.sponsorship` values (§2.2)
SPONSORSHIP = ("yes", "no", "undeclared")
#: statement kinds that impose no skill demand: a `skill` mention linked only
#: to these is the §3 warning (the Toronto case under the new contract)
NON_DEMAND_KINDS = frozenset({"employment_constraint", "compensation_statement",
                              "employer_context", "hiring_policy"})

# statement kinds that carry a non-null importance (spec §3: qualifications and
# employment constraints; hiring policies impose applicant rules the same way)
IMPORTANCE_KINDS = frozenset({"qualification", "employment_constraint", "hiring_policy"})

#: What a statement with NO importance projects as in the legacy columns.
#: Responsibilities, compensation statements and employer context carry a null
#: importance under schema 2 (spec §3), and under schema 3 no statement carries
#: one at all (parsing contract v3 §2.1) — the field was a verdict the model
#: assigned from descriptor text and nothing in the source could check it. The
#: `profile_mentions` column is NOT NULL, and `contextual` is v1's existing word
#: for "named by the posting, not demanded by it", which is exactly what a
#: statement with no verdict asserts. Lives here rather than in `serve` because
#: `project` needs it too and `serve` imports `project`.
NO_IMPORTANCE = "contextual"


@dataclass(frozen=True)
class Block:
    """One nonempty source line under blocks/1: id, exact text, codepoint span."""

    id: str
    text: str
    span: tuple[int, int]
