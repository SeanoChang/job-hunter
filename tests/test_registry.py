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
    assert snap.startswith(b'[{"board":"ramp","company":"Ramp","country":"US","extra":{}')


@pytest.mark.parametrize(
    "bad,msg",
    [
        ('[[boards]]\ncompany="X"\nsource="notasource"\nboard="x"\n', "unknown source"),
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


WORKDAY_GOOD = """
[[boards]]
company = "NVIDIA"
source  = "workday"
board   = "nvidia"
host    = "wd5"
site    = "NVIDIAExternalCareerSite"
"""

ORACLEHCM_GOOD = """
[[boards]]
company = "JPMorgan Chase"
source  = "oraclehcm"
board   = "jpmc"
base    = "https://jpmc.fa.oraclecloud.com"
site    = "CX_1001"
"""

EIGHTFOLD_GOOD = """
[[boards]]
company = "Netflix"
source  = "eightfold"
board   = "netflix"
base    = "https://explore.jobs.netflix.net"
domain  = "netflix.com"
"""


def test_workday_board_with_valid_extra_loads(tmp_path: Path) -> None:
    reg = load(_write(tmp_path, WORKDAY_GOOD))
    assert reg.boards[0].extra == {"host": "wd5", "site": "NVIDIAExternalCareerSite"}


def test_oraclehcm_board_with_valid_extra_loads(tmp_path: Path) -> None:
    reg = load(_write(tmp_path, ORACLEHCM_GOOD))
    assert reg.boards[0].extra == {
        "base": "https://jpmc.fa.oraclecloud.com",
        "site": "CX_1001",
    }


def test_eightfold_board_with_valid_extra_loads(tmp_path: Path) -> None:
    reg = load(_write(tmp_path, EIGHTFOLD_GOOD))
    assert reg.boards[0].extra == {
        "base": "https://explore.jobs.netflix.net",
        "domain": "netflix.com",
    }


def test_workday_board_missing_host_extra_key_fails_with_teaching_error(tmp_path: Path) -> None:
    bad = WORKDAY_GOOD.replace('host    = "wd5"\n', "")
    with pytest.raises(RegistryError) as exc_info:
        load(_write(tmp_path, bad))
    msg = str(exc_info.value)
    assert "host" in msg
    assert "nvidia" in msg
    assert "site" in msg  # a valid entry example is shown


def test_workday_board_missing_site_extra_key_fails_with_teaching_error(tmp_path: Path) -> None:
    bad = WORKDAY_GOOD.replace('site    = "NVIDIAExternalCareerSite"\n', "")
    with pytest.raises(RegistryError) as exc_info:
        load(_write(tmp_path, bad))
    msg = str(exc_info.value)
    assert "site" in msg
    assert "nvidia" in msg


def test_oraclehcm_board_missing_base_extra_key_fails_with_teaching_error(tmp_path: Path) -> None:
    bad = ORACLEHCM_GOOD.replace('base    = "https://jpmc.fa.oraclecloud.com"\n', "")
    with pytest.raises(RegistryError) as exc_info:
        load(_write(tmp_path, bad))
    msg = str(exc_info.value)
    assert "base" in msg
    assert "jpmc" in msg


def test_oraclehcm_board_missing_site_extra_key_fails_with_teaching_error(tmp_path: Path) -> None:
    bad = ORACLEHCM_GOOD.replace('site    = "CX_1001"\n', "")
    with pytest.raises(RegistryError) as exc_info:
        load(_write(tmp_path, bad))
    msg = str(exc_info.value)
    assert "site" in msg
    assert "jpmc" in msg


def test_eightfold_board_missing_base_extra_key_fails_with_teaching_error(tmp_path: Path) -> None:
    bad = EIGHTFOLD_GOOD.replace('base    = "https://explore.jobs.netflix.net"\n', "")
    with pytest.raises(RegistryError) as exc_info:
        load(_write(tmp_path, bad))
    msg = str(exc_info.value)
    assert "base" in msg
    assert "netflix" in msg


def test_eightfold_board_missing_domain_extra_key_fails_with_teaching_error(tmp_path: Path) -> None:
    bad = EIGHTFOLD_GOOD.replace('domain  = "netflix.com"\n', "")
    with pytest.raises(RegistryError) as exc_info:
        load(_write(tmp_path, bad))
    msg = str(exc_info.value)
    assert "domain" in msg
    assert "netflix" in msg


def test_unknown_extra_key_on_workday_board_is_an_error(tmp_path: Path) -> None:
    bad = WORKDAY_GOOD + 'region  = "us-east"\n'
    with pytest.raises(RegistryError, match="region"):
        load(_write(tmp_path, bad))


def test_unknown_extra_key_on_greenhouse_board_is_an_error(tmp_path: Path) -> None:
    bad = GOOD + '\n[[boards]]\ncompany="X"\nsource="greenhouse"\nboard="x"\nhost="wd5"\n'
    with pytest.raises(RegistryError, match="host"):
        load(_write(tmp_path, bad))


def test_board_extra_defaults_to_empty_mapping(tmp_path: Path) -> None:
    reg = load(_write(tmp_path, GOOD))
    assert dict(reg.boards[0].extra) == {}


def test_board_extra_mapping_is_immutable(tmp_path: Path) -> None:
    reg = load(_write(tmp_path, WORKDAY_GOOD))
    with pytest.raises(TypeError):
        reg.boards[0].extra["host"] = "wd1"  # type: ignore[index]


def test_revision_changes_when_extra_site_value_changes(tmp_path: Path) -> None:
    a = load(_write(tmp_path, WORKDAY_GOOD)).revision
    b = load(
        _write(tmp_path, WORKDAY_GOOD.replace("NVIDIAExternalCareerSite", "OtherSite"))
    ).revision
    assert a != b


def test_extra_value_must_be_a_non_empty_string(tmp_path: Path) -> None:
    bad = WORKDAY_GOOD.replace('host    = "wd5"\n', "host    = \"\"\n")
    with pytest.raises(RegistryError, match="host"):
        load(_write(tmp_path, bad))
