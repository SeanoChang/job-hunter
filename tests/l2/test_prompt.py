import re

from jobhunter.l2.prompt import PROMPT_VERSION, TEMPLATE, prompt_sha, render


def test_version_and_sha() -> None:
    assert PROMPT_VERSION == "demand-profile/v1"
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
