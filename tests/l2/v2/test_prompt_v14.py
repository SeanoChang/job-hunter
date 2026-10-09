"""`demand-profile/v14`: v13 with a narrower own-product rule.

v13 typed "the employer's own product" as an organization, reasoning that a
candidate cannot bring it from elsewhere. Over 1,219 entry-level postings that
put AWS under organization in 145 Amazon postings, Figma in 72 Figma postings,
Databricks in 21 Databricks postings and Snowflake in 7 — public platforms a
candidate can bring from anywhere — so a skill filter for AWS missed Amazon's
own jobs. v14 keeps only internal products (Visa Checkout) as organizations.
"""

from __future__ import annotations

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2 import prompt_v13, prompt_v14
from tests.l2.v2.test_prompt_v12 import V12_SHA

#: v13's template as bundle v3 shipped it on 2026-10-07 (its live entry-level run)
V13_SHA = prompt_v13.prompt_sha()


def test_version_and_sha() -> None:
    assert prompt_v14.PROMPT_VERSION == "demand-profile/v14"
    assert prompt_v14.prompt_sha() == sha256_hex(prompt_v14.TEMPLATE.encode("utf-8"))


def test_older_bytes_are_untouched() -> None:
    from jobhunter.l2.v2 import prompt_v12

    assert prompt_v12.prompt_sha() == V12_SHA
    assert prompt_v13.prompt_sha() == sha256_hex(prompt_v13.TEMPLATE.encode("utf-8"))


def test_only_the_mention_rules_change() -> None:
    restored = prompt_v14.TEMPLATE.replace(prompt_v14._MENTION_RULES, prompt_v13._MENTION_RULES)
    assert restored == prompt_v13.TEMPLATE
    assert prompt_v14._MENTION_RULES != prompt_v13._MENTION_RULES


def test_a_platform_its_maker_sells_stays_a_skill() -> None:
    rules = prompt_v14._MENTION_RULES
    assert "AWS in an Amazon posting" in rules
    assert "Databricks" in rules and "Figma" in rules
    assert "cannot bring it from elsewhere" not in rules


def test_only_internal_products_are_organizations() -> None:
    rules = prompt_v14._MENTION_RULES
    assert "Visa Checkout" in rules and "internal" in rules


def test_the_v13_name_rule_and_recall_rule_carry_over() -> None:
    rules = prompt_v14._MENTION_RULES
    assert "Generic nouns are not names" in rules
    assert "ANY statement" in rules and "responsibility lines" in rules
