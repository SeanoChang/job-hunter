import re

from jobhunter.l2.prompt import PROMPT_VERSION, TEMPLATE, prompt_sha, render


def test_version_and_sha() -> None:
    assert PROMPT_VERSION == "demand-profile/v2"
    assert re.fullmatch(r"[0-9a-f]{64}", prompt_sha())
    assert prompt_sha() == prompt_sha()  # stable


def test_render_embeds_document_verbatim() -> None:
    md = "## 応募資格\n\n- **Python** and Go"
    out = render(md, [])
    assert "<<<\n" + md + "\n>>>" in out
    assert "previous answer failed validation" not in out


def test_render_prior_errors_block() -> None:
    out = render("doc", ["quote not found: 'x'", "ambiguous quote: 'y'"])
    assert "Your previous answer failed validation:" in out
    assert "- quote not found: 'x'" in out
    assert "- ambiguous quote: 'y'" in out
    assert out.index("doc") < out.index("failed validation")


def test_template_is_the_hashed_bytes() -> None:
    from jobhunter.hashing import sha256_hex

    assert prompt_sha() == sha256_hex(TEMPLATE.encode("utf-8"))


def test_v2_rules_present() -> None:
    """v2 exists because a real run failed twice on these two points:
    the model anchored a `deadline` fact on "Deadline to apply: None", and it
    paraphrased evidence fragments instead of copying them."""
    lowered = TEMPLATE.lower()
    # facts: an explicit absence means null, not an anchor on the denial
    assert "only when the posting states an actual value" in lowered
    assert "deadline to apply: none" in lowered   # the exact trap, named
    assert "null" in lowered
    # evidence fragments must be copied, not paraphrased
    assert "character-for-character" in lowered
    assert "level_evidence" in lowered
