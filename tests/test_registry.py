from pathlib import Path

import pytest

from jobhunter.registry import RegistryError, load

GOOD = """
[[boards]]
company = "Anthropic"
source  = "greenhouse"
board   = "anthropic"

[[boards]]
company = "Ramp"
source  = "ashby"
board   = "ramp"
country = "US"
tags    = ["fintech"]
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "companies.toml"
    p.write_text(text)
    return p


def test_load_parses_boards_in_sorted_order(tmp_path: Path) -> None:
    reg = load(_write(tmp_path, GOOD))
    assert [b.key for b in reg.boards] == ["ashby:ramp", "greenhouse:anthropic"]
    assert reg.boards[0].tags == ("fintech",)
    assert reg.boards[0].country == "US"


def test_revision_is_stable_across_formatting(tmp_path: Path) -> None:
    a = load(_write(tmp_path, GOOD)).revision
    b = load(_write(tmp_path, GOOD.replace("  ", " ") + "\n# comment\n")).revision
    assert a == b and len(a) == 64


def test_revision_changes_when_a_board_changes(tmp_path: Path) -> None:
    a = load(_write(tmp_path, GOOD)).revision
    b = load(_write(tmp_path, GOOD.replace('"ramp"', '"ramp2"'))).revision
    assert a != b


def test_snapshot_json_is_canonical(tmp_path: Path) -> None:
    snap = load(_write(tmp_path, GOOD)).snapshot_json()
    assert snap.startswith(b'[{"board":"ramp","company":"Ramp","country":"US"')


@pytest.mark.parametrize(
    "bad,msg",
    [
        ('[[boards]]\ncompany="X"\nsource="workday"\nboard="x"\n', "unknown source"),
        ('[[boards]]\ncompany="X"\nsource="lever"\nboard="bad board"\n', "board"),
        ('[[boards]]\ncompany=""\nsource="lever"\nboard="x"\n', "company"),
        (
            '[[boards]]\ncompany="X"\nsource="lever"\nboard="x"\n'
            '[[boards]]\ncompany="Y"\nsource="lever"\nboard="x"\n',
            "duplicate",
        ),
        ("boards = 3\n", "boards"),
    ],
)
def test_validation_errors(tmp_path: Path, bad: str, msg: str) -> None:
    with pytest.raises(RegistryError, match=msg):
        load(_write(tmp_path, bad))


def test_missing_file_is_a_registry_error(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="not found"):
        load(tmp_path / "absent.toml")


def test_board_with_trailing_newline_is_rejected(tmp_path: Path) -> None:
    bad = '[[boards]]\ncompany="X"\nsource="lever"\nboard="""x\n"""\n'
    with pytest.raises(RegistryError, match="board"):
        load(_write(tmp_path, bad))


def test_source_must_be_a_string(tmp_path: Path) -> None:
    bad = '[[boards]]\ncompany="X"\nsource=["lever"]\nboard="x"\n'
    with pytest.raises(RegistryError, match="source"):
        load(_write(tmp_path, bad))
