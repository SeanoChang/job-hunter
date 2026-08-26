import pytest

from jobhunter.l2.quotes import (
    AmbiguousQuote,
    QuoteNotFound,
    find_occurrences,
    line_col,
    longest_matching_prefix,
    occurrence_index,
    resolve_quote,
)

MD = "## Skills\n\n- **Python** and Go\n- Python for scripting\n\n5年以上の経験 🎯 required"


def test_find_occurrences() -> None:
    assert find_occurrences(MD, "Python") == [15, 33]
    assert find_occurrences(MD, "absent") == []


def test_resolve_unique() -> None:
    q = resolve_quote(MD, "**Python** and Go")
    assert q.span == (13, 30)
    assert q.occurrence == 0
    assert MD[q.span[0] : q.span[1]] == q.text


def test_resolve_ambiguous_needs_occurrence() -> None:
    with pytest.raises(AmbiguousQuote) as exc:
        resolve_quote(MD, "Python")
    assert exc.value.starts == [15, 33]
    q = resolve_quote(MD, "Python", occurrence=1)
    assert q.span == (33, 39)
    with pytest.raises(AmbiguousQuote):
        resolve_quote(MD, "Python", occurrence=2)


def test_resolve_not_found_prefix_diagnostic() -> None:
    with pytest.raises(QuoteNotFound) as exc:
        resolve_quote(MD, "Python for scripts")
    assert exc.value.longest_prefix == len("Python for script")


def test_longest_matching_prefix() -> None:
    assert longest_matching_prefix(MD, "Python for scripts") == 17
    assert longest_matching_prefix(MD, "zzz absent") == 0
    assert longest_matching_prefix(MD, "Python") == 6


def test_cjk_and_astral_are_codepoints() -> None:
    q = resolve_quote(MD, "5年以上の経験")
    assert q.span == (55, 62)
    assert MD[q.span[0] : q.span[1]] == "5年以上の経験"
    emoji = resolve_quote(MD, "🎯")
    assert emoji.span[1] - emoji.span[0] == 1  # one codepoint, not a UTF-16 pair


def test_occurrence_index() -> None:
    assert occurrence_index(MD, "Python", 15) == 0
    assert occurrence_index(MD, "Python", 33) == 1
    assert occurrence_index(MD, "Python", 14) == -1


def test_line_col() -> None:
    assert line_col(MD, 0) == (1, 1)
    assert line_col(MD, 12) == (3, 2)
    assert line_col(MD, 15) == (3, 5)
