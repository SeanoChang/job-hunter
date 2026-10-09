"""`demand-profile/v15`: v14 with an unconditional-only citizenship rule and no
social-channel mentions.

The 2026-10-08 v14 test run (17 postings) read Databricks' standard
export-control paragraph — "If access to export-controlled technology or
source code is required for performance of job duties, it is within
Employer's discretion whether to apply for a U.S. government license ... and
Employer may decline to proceed with an applicant" — as citizenship_required,
which would hide Databricks postings from an international student's filter.
The same run typed the footer's "LinkedIn", "X", "YouTube", "Instagram" as
skills.
"""

from __future__ import annotations

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2 import prompt_v12, prompt_v14, prompt_v15


def test_version_and_sha() -> None:
    assert prompt_v15.PROMPT_VERSION == "demand-profile/v15"
    assert prompt_v15.prompt_sha() == sha256_hex(prompt_v15.TEMPLATE.encode("utf-8"))


def test_only_the_authorization_and_mention_rules_change() -> None:
    restored = (prompt_v15.TEMPLATE
                .replace(prompt_v15._AUTHORIZATION_RULES, prompt_v12._AUTHORIZATION_RULES)
                .replace(prompt_v15._MENTION_RULES, prompt_v14._MENTION_RULES))
    assert restored == prompt_v14.TEMPLATE
    assert prompt_v15._AUTHORIZATION_RULES != prompt_v12._AUTHORIZATION_RULES
    assert prompt_v15._MENTION_RULES != prompt_v14._MENTION_RULES


def test_citizenship_is_positive_only_when_unconditional() -> None:
    rules = " ".join(prompt_v15._AUTHORIZATION_RULES.split())
    assert "unconditional" in rules
    assert "export-controlled technology" in rules  # the Databricks paragraph, as the example
    assert "ambiguous" in rules


def test_the_v12_authorization_rulings_carry_over() -> None:
    rules = " ".join(prompt_v15._AUTHORIZATION_RULES.split())
    assert "sponsorship may be available" in rules
    assert "work_authorization only" in rules
    assert "never" in rules and "sponsorship" in rules


def test_social_channels_are_not_mentions() -> None:
    rules = prompt_v15._MENTION_RULES
    for name in ("LinkedIn", "YouTube", "Instagram"):
        assert name in rules
    assert "AWS in an Amazon posting" in rules  # v14's platform rule carries over
