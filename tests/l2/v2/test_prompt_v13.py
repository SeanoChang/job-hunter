"""`demand-profile/v13`: v12 with a skill that is a name, not a noun.

The 2026-10-07 live run of bundle v3 (v12) on four entry-level postings
recalled Kafka, Docker and Kubernetes from a duty line as asked, and also
typed generic nouns as skills: Figma's "tools", "experiments",
"infrastructure", "collaboration tools"; Anduril's "flight software
algorithms", "software/hardware components", "simulation models"; Visa's
"Chatbot" and its own product "Visa Checkout". v13 changes exactly one
section, the mention rules, so the rest of v12's bytes carry over.
"""

from __future__ import annotations

from jobhunter.hashing import sha256_hex
from jobhunter.l2.v2 import prompt_v12, prompt_v13
from tests.l2.v2.test_prompt_v12 import V12_SHA


def test_version_and_sha() -> None:
    assert prompt_v13.PROMPT_VERSION == "demand-profile/v13"
    assert prompt_v13.prompt_sha() == sha256_hex(prompt_v13.TEMPLATE.encode("utf-8"))


def test_v12_bytes_are_untouched() -> None:
    """The four v12 attempts in the local archive stay replayable under their
    own bytes."""
    assert prompt_v12.prompt_sha() == V12_SHA


def test_only_the_mention_rules_change() -> None:
    restored = prompt_v13.TEMPLATE.replace(
        prompt_v13._MENTION_RULES, prompt_v12._MENTION_RULES
    )
    assert restored == prompt_v12.TEMPLATE
    assert prompt_v13._MENTION_RULES != prompt_v12._MENTION_RULES


def test_a_mention_is_a_name_and_generic_nouns_are_not() -> None:
    rules = prompt_v13._MENTION_RULES
    assert "Generic nouns are not names" in rules
    for noun in ('"tools"', '"infrastructure"', '"experiments"', '"components"'):
        assert noun in rules
    assert '"Kafka clusters"' in rules and "Kafka" in rules


def test_the_employers_own_product_is_never_a_skill() -> None:
    rules = prompt_v13._MENTION_RULES
    assert "employer's own product" in rules
    assert "organization" in rules


def test_the_recall_rule_still_covers_every_statement_kind() -> None:
    rules = prompt_v13._MENTION_RULES
    assert "responsibility lines" in rules and "ANY statement" in rules


def test_render_puts_the_document_in_the_v13_template() -> None:
    out = prompt_v13.render("Build Kafka pipelines.", [], None)
    assert out.startswith(prompt_v13.TEMPLATE.split("{source_blocks}")[0])
    assert "Build Kafka pipelines." in out
