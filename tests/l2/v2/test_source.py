import pytest

from jobhunter.l2.v2.source import (
    ANNOTATION_VERSION,
    RefBindError,
    annotate,
    blocks_by_id,
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


def test_single_occurrence_slip_is_owned_by_code() -> None:
    # parsing-rules/6: a block with exactly one occurrence makes any emitted
    # index a labeling slip; two occurrences still refuse a bad index
    md = "# T\n\n3+ years of Go required.\n\nGo and Go again."
    blocks = blocks_by_id(annotate(md))
    bound = resolve({"block_id": "b000002", "text": "3+ years", "occurrence": 1}, blocks)
    assert bound["occurrence"] == 0
    with pytest.raises(RefBindError):
        resolve({"block_id": "b000003", "text": "Go", "occurrence": 5}, blocks)
