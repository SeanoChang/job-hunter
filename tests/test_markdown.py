import html
import json
from pathlib import Path

import pytest

from jobhunter.markdown import NORMALIZER_VERSION, strip_markdown, to_markdown, visible_text
from tests.conftest import FIXTURES

GOLDEN = FIXTURES / "md"


def _fixture_htmls() -> dict[str, str]:
    gh = json.loads((FIXTURES / "greenhouse_board.json").read_text())["jobs"][0]
    ab = json.loads((FIXTURES / "ashby_board.json").read_text())["jobs"][0]
    lv = json.loads((FIXTURES / "lever_board.json").read_text())[0]
    proto = Path(__file__).resolve().parents[1] / "prototypes" / "parsing" / "fixtures"
    notion = json.loads((proto / "linkedin_notion_early-career-ai.json").read_text())
    nvidia = json.loads((proto / "workday_nvidia_backend-compiler.json").read_text())
    from jobhunter.sources.lever import _description

    return {
        "greenhouse_anthropic": html.unescape(gh["content"]),
        "ashby_ramp": ab["descriptionHtml"],
        "lever_palantir": _description(lv),
        "linkedin_notion": notion["descriptionHtml"],
        "workday_nvidia": nvidia["descriptionHtml"],
    }


def test_normalizer_version() -> None:
    assert NORMALIZER_VERSION == "md/1"


@pytest.mark.parametrize(
    "src,expected",
    [
        ("<h2><strong>About</strong></h2><p>Hi <em>there</em>.</p>", "## **About**\n\nHi *there*."),
        ("<ul><li>a</li><li>b<ul><li>c</li></ul></li></ul>", "- a\n- b\n  - c"),
        ("<ol><li>x</li><li>y</li></ol>", "1. x\n2. y"),
        ('<p>See <a href="https://x">here</a>.</p>', "See [here](https://x)."),
        ("<div><div><p>nested</p></div></div>", "nested"),
        ("<p>line<br>break</p>", "line\nbreak"),
        ("<p>a</p><hr><p>b</p>", "a\n\n---\n\nb"),
        ("<script>x()</script><p>only</p><style>p{}</style>", "only"),
        ("<p>ﬁ ①</p>", "fi 1"),  # NFKC
        ("<li><p>para in li</p><p>second</p></li>", "- para in li second"),
        ("<p>  many    spaces \n here </p>", "many spaces here"),
        ("<p></p><div></div><p>kept</p>", "kept"),
    ],
)
def test_small_cases(src: str, expected: str) -> None:
    assert to_markdown(src) == expected


def test_idempotent_whitespace_and_no_trailing() -> None:
    md = to_markdown("<p>a</p>\n\n\n<p>b</p>   ")
    assert md == "a\n\nb"
    assert not any(line != line.rstrip() for line in md.splitlines())


@pytest.mark.parametrize("name", sorted(_fixture_htmls()))
def test_goldens(name: str) -> None:
    md = to_markdown(_fixture_htmls()[name])
    golden = (GOLDEN / f"{name}.md").read_text(encoding="utf-8")
    assert md == golden, f"golden drift for {name}; regenerate deliberately if md/1 changed"


@pytest.mark.parametrize("name", sorted(_fixture_htmls()))
def test_text_is_preserved(name: str) -> None:
    src = _fixture_htmls()[name]
    assert strip_markdown(to_markdown(src)) == visible_text(src)


@pytest.mark.parametrize("name", sorted(_fixture_htmls()))
def test_structure_survives(name: str) -> None:
    md = to_markdown(_fixture_htmls()[name])
    assert "\n- " in md or "\n1. " in md or md.startswith("- ")  # every posting has lists
    assert "<" not in md.replace("<=", "")  # no leftover tags


def test_table_cells_do_not_glue() -> None:
    html_table = "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>"
    assert to_markdown(html_table) == "a b\n\nc"


def test_source_nul_is_not_mistaken_for_br() -> None:
    assert to_markdown("<p>x\x00y</p>") == "x\x00y"


@pytest.mark.parametrize(
    "src,expected",
    [
        ("<p><b>x</b> <b>y</b></p>", "**x** **y**"),
        ("<p><em>foo</em> <em>bar</em></p>", "*foo* *bar*"),
        (
            "<p><strong>Salary:</strong> <strong>competitive</strong></p>",
            "**Salary:** **competitive**",
        ),
        ("<ul><li><b>A</b> <b>B</b></li></ul>", "- **A** **B**"),
        ("<p><b></b>kept</p>", "kept"),
    ],
)
def test_adjacent_emphasis_is_not_corrupted(src: str, expected: str) -> None:
    md = to_markdown(src)
    assert md == expected
    assert strip_markdown(md) == visible_text(src)
