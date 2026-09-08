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
