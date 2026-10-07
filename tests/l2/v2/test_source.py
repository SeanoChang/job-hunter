import pytest

from jobhunter.l2.v2.source import (
    ANNOTATION_VERSION,
    RefBindError,
    annotate,
    blocks_by_id,
    heading_of,
    is_heading,
    resolve,
)

MD = "# Title\n\nPython and SQL. Python daily.\n   \n最低8年の経験。\n"


def test_annotation_version() -> None:
    assert ANNOTATION_VERSION == "blocks/1"


def test_annotate_ids_spans_and_blanks() -> None:
    blocks = annotate(MD)
    assert [b.id for b in blocks] == ["b000001", "b000002", "b000003"]
    assert blocks[0].text == "# Title" and blocks[0].span == (0, 7)
    # blank and whitespace-only lines get no block; offsets stay codepoint-true
    assert blocks[1].text == "Python and SQL. Python daily."
    assert MD[blocks[1].span[0] : blocks[1].span[1]] == blocks[1].text
    assert MD[blocks[2].span[0] : blocks[2].span[1]] == "最低8年の経験。"  # CJK codepoints


def test_annotate_empty_document() -> None:
    assert annotate("") == []
    assert annotate("\n \n") == []


def test_resolve_whole_block() -> None:
    blocks = blocks_by_id(annotate(MD))
    q = resolve({"block_id": "b000001", "text": None, "occurrence": None}, blocks)
    assert q == {"block_id": "b000001", "text": "# Title", "span": [0, 7], "occurrence": 0}


def test_resolve_substring_occurrence() -> None:
    blocks = blocks_by_id(annotate(MD))
    q = resolve({"block_id": "b000002", "text": "Python", "occurrence": 1}, blocks)
    s, e = q["span"]
    assert MD[s:e] == "Python" and q["occurrence"] == 1


@pytest.mark.parametrize(
    "ref",
    [
        {"block_id": "b000099", "text": None, "occurrence": None},   # unknown block
        {"block_id": "b000002", "text": "Ruby", "occurrence": 0},    # not a substring
        {"block_id": "b000002", "text": "Python", "occurrence": 5},  # impossible index
        {"block_id": "b000002", "text": "Python", "occurrence": None},  # half-null pair
        {"block_id": "b000002", "text": None, "occurrence": 0},         # half-null pair
    ],
)
def test_resolve_rejects(ref: dict[str, object]) -> None:
    blocks = blocks_by_id(annotate(MD))
    with pytest.raises(RefBindError):
        resolve(ref, blocks)


def test_reanchor_binds_a_unique_exact_match_elsewhere() -> None:
    # parsing-rules/3: a verbatim quote citing the wrong block binds to the
    # one block that actually contains it; ambiguity still refuses
    md = "# Basic Qualifications:\n\n5 years of Go.\n\nNice to have: Rust."
    blocks = blocks_by_id(annotate(md))
    wrong = {"block_id": "b000002", "text": "Basic Qualifications:", "occurrence": 0}
    bound = resolve(wrong, blocks)
    assert bound["block_id"] == "b000001"
    assert md[bound["span"][0]:bound["span"][1]] == "Basic Qualifications:"


def test_reanchor_refuses_ambiguity_and_absence() -> None:
    md = "# Title\n\nGo experience.\n\nGo tooling."
    blocks = blocks_by_id(annotate(md))
    with pytest.raises(RefBindError):  # "Go" occurs in two other blocks
        resolve({"block_id": "b000001", "text": "Go", "occurrence": 0}, blocks)
    with pytest.raises(RefBindError):  # absent text still refuses
        resolve({"block_id": "b000001", "text": "Rust", "occurrence": 0}, blocks)


def test_reanchor_never_fires_for_nonzero_occurrence() -> None:
    md = "# T\n\nGo and Go.\n\nGo again."
    blocks = blocks_by_id(annotate(md))
    with pytest.raises(RefBindError):
        resolve({"block_id": "b000003", "text": "and", "occurrence": 1}, blocks)


def test_typographic_tiers_bind_document_bytes() -> None:
    # parsing-rules/5: the model normalizes typography; the document is truth
    md = "# T\n\nWe’re committed to fair hiring.\n\nBody text."
    blocks = blocks_by_id(annotate(md))
    bound = resolve(
        {"block_id": "b000002", "text": "We're committed to fair hiring.", "occurrence": 0},
        blocks,
    )
    assert bound["text"] == "We’re committed to fair hiring."
    assert md[bound["span"][0]:bound["span"][1]] == bound["text"]


def test_lenient_mentions_bind_first_casefold_match() -> None:
    md = "# Senior Engineer\n\nzendesk builds support software.\n\nzendesk is remote."
    blocks = blocks_by_id(annotate(md))
    with pytest.raises(RefBindError):  # strict path: ambiguous casefold
        resolve({"block_id": "b000001", "text": "Zendesk", "occurrence": 0}, blocks)
    bound = resolve({"block_id": "b000001", "text": "Zendesk", "occurrence": 0},
                    blocks, lenient=True)
    assert bound["block_id"] == "b000002" and bound["text"] == "zendesk"


def test_emphasis_fold_binds_original_bytes() -> None:
    # parsing-rules/6: quotes that dropped the document's markers still bind,
    # and the span covers the original bytes, markers included
    md = "# T\n\nbuilding and maintaining **Workday FIN Reporting & Analytics** at scale."
    blocks = blocks_by_id(annotate(md))
    bound = resolve(
        {"block_id": "b000002",
         "text": "building and maintaining Workday FIN Reporting & Analytics",
         "occurrence": 0},
        blocks,
    )
    # the span covers the document's bytes for the matched characters —
    # interior markers included, trailing markers (after the last matched
    # character) naturally excluded
    assert bound["text"] == "building and maintaining **Workday FIN Reporting & Analytics"
    assert md[bound["span"][0]:bound["span"][1]] == bound["text"]


def test_casefold_expansion_stays_inside_the_fold_map() -> None:
    # 2026-09-22 drain crash: casefold can EXPAND a character ('ß' -> 'ss'),
    # so the folded haystack outgrew a per-original-char index map and a match
    # reaching past the last original character indexed off its end
    # (IndexError at source.py:110, killing the whole parallel drain). The
    # map must carry one entry per FOLDED character; the bound span still
    # covers the original bytes.
    md = "# T\n\nDu bringst sehr gutes Deutsch mit und arbeitest mit **Fleiß**"
    blocks = blocks_by_id(annotate(md))
    bound = resolve(
        {"block_id": "b000002",
         "text": "arbeitest mit FLEISS",
         "occurrence": 0},
        blocks,
    )
    assert bound["text"] == "arbeitest mit **Fleiß"
    assert md[bound["span"][0]:bound["span"][1]] == bound["text"]


def test_single_occurrence_slip_is_owned_by_code() -> None:
    # parsing-rules/6: a block with exactly one occurrence makes any emitted
    # index a labeling slip; two occurrences still refuse a bad index
    md = "# T\n\n3+ years of Go required.\n\nGo and Go again."
    blocks = blocks_by_id(annotate(md))
    bound = resolve({"block_id": "b000002", "text": "3+ years", "occurrence": 1}, blocks)
    assert bound["occurrence"] == 0
    with pytest.raises(RefBindError):
        resolve({"block_id": "b000003", "text": "Go", "occurrence": 5}, blocks)


# --- the heading predicate (parsing contract v3 §2.1) -----------------------
# `section_heading` is code-owned: a heading is block structure the document
# already carries, so the model never emits one and nothing has to verify a
# model's copy of it. The predicate below is the whole definition.

HEADING_MD = (
    "# Senior Engineer\n"          # b000001 ATX
    "About the team\n"             # b000002 plain paragraph
    "## Requirements\n"            # b000003 ATX
    "- 5 years of Go\n"            # b000004 bullet
    "**Nice to have:**\n"          # b000005 bold-only line
    "- Rust\n"                     # b000006 bullet
    "We look for **strong** writers.\n"  # b000007 bold phrase inside a sentence
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Senior Engineer", True),
        ("###### Deep", True),
        ("## Requirements", True),
        ("**Nice to have:**", True),
        ("**Requirements**", True),
        ("**Requirements**:", True),
        ("## **Requirements**", True),         # ATX whose text is bold
        ("**:**", True),                       # degenerate: a label of pure punctuation
        ("## :", True),
        ("- 5 years of Go", False),            # a bullet is not a heading
        ("- **Bonus:** Rust", False),          # nor a bullet that starts bold
        ("About the team", False),             # nor a plain paragraph
        ("We look for **strong** writers.", False),  # nor bold inside a sentence
        ("#NoSpace", False),                   # ATX needs a space
        ("####### Seven", False),              # seven hashes is not ATX
        ("#", False),                          # no content
        ("**" + "x" * 81 + "**", False),       # past the bold-line bound
        ("", False),
    ],
)
def test_is_heading_predicate(text: str, expected: bool) -> None:
    assert is_heading(text) is expected


def test_heading_of_takes_the_nearest_preceding_heading() -> None:
    blocks = annotate(HEADING_MD)
    # the bullet under "## Requirements" — marks stripped, nearest wins over
    # the document title above it
    assert heading_of(blocks, "b000004") == "Requirements"
    # a plain paragraph under the title
    assert heading_of(blocks, "b000002") == "Senior Engineer"
    # a bold-only line is a heading, and its trailing colon is punctuation
    assert heading_of(blocks, "b000006") == "Nice to have"
    # the sentence with bold inside it is not itself a heading, so it still
    # belongs to the bold-only heading above it
    assert heading_of(blocks, "b000007") == "Nice to have"


def test_heading_of_is_null_when_none_precedes() -> None:
    blocks = annotate("About the team\nWe build things.\n## Requirements\n")
    assert heading_of(blocks, "b000001") is None
    assert heading_of(blocks, "b000002") is None
    assert heading_of(blocks, "b000099") is None  # unknown block: null, never a guess


def test_heading_of_a_heading_block_is_its_own_heading() -> None:
    # a heading opens the section it names: for the degenerate case where the
    # cited block IS a heading, its own text is the only true answer — the
    # heading above it belongs to the section this one ends
    blocks = annotate("## About us\n## Requirements\n- 5 years of Go\n")
    assert heading_of(blocks, "b000002") == "Requirements"
    assert heading_of(blocks, "b000003") == "Requirements"


def test_heading_of_strips_only_heading_syntax() -> None:
    blocks = annotate("## Learn C#\n- ship it\n### Basic Qualifications: ###\n- own it\n")
    assert heading_of(blocks, "b000002") == "Learn C#"  # a trailing C# survives
    assert heading_of(blocks, "b000004") == "Basic Qualifications"  # closing ATX run


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("**Requirements:**", "Requirements"),   # colon inside the bold run
        ("**Requirements**:", "Requirements"),   # colon after it
        ("**Requirements**", "Requirements"),
        ("**  Nice to have  **", "Nice to have"),
        # An ATX line whose text is bold is the dominant heading spelling in
        # the recorded corpus (9 of 9 ATX headings in
        # tests/fixtures/md/greenhouse_anthropic.md, 7 of 10 in ashby_ramp.md).
        # It names the same section as the bold-only spelling, so it must
        # derive the same label: the markup is not part of the name.
        ("## **Requirements:**", "Requirements"),
        ("## **Requirements**", "Requirements"),
        ("## **Requirements**:", "Requirements"),
        ("# **About Ramp**", "About Ramp"),
        ("### **Required Qualifications:**", "Required Qualifications"),
        ("## *Requirements*", "Requirements"),      # one italic run, same rule
        ("## __Requirements__", "Requirements"),
        # a run that does not wrap the WHOLE label is content, not syntax
        ("## **Required** or **Preferred**", "**Required** or **Preferred**"),
        ("## Learn C#", "Learn C#"),
    ],
)
def test_heading_of_reads_every_heading_spelling(heading: str, expected: str) -> None:
    blocks = annotate(f"{heading}\n- Rust\n")
    assert heading_of(blocks, "b000002") == expected


@pytest.mark.parametrize("heading", ["**:**", "**  **", "## :", "#  :", "### **:**"])
def test_heading_of_is_null_for_a_heading_whose_label_is_only_punctuation(
    heading: str,
) -> None:
    """A heading that names nothing answers null, never "".

    The record schema rejects an empty `section_heading` (`minLength: 1`), so
    without this guard a degenerate heading would turn an otherwise clean
    record into `schema:invalid`. Null over an empty string.
    """
    blocks = annotate(f"{heading}\n- Rust\n")
    assert heading_of(blocks, "b000001") is None
    assert heading_of(blocks, "b000002") is None


def test_a_heading_naming_nothing_ends_the_section_above_it() -> None:
    # it is still a heading under the predicate, so it closes "Requirements";
    # the section it opens has no name, and null over guess means the bullet
    # under it is not filed under a section it no longer sits in
    blocks = annotate("## Requirements\n- 5 years of Go\n**  **\n- Rust\n")
    assert heading_of(blocks, "b000002") == "Requirements"
    assert heading_of(blocks, "b000004") is None
