from pathlib import Path

import pytest

from jobhunter.models import Board

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def boards() -> dict[str, Board]:
    return {
        "greenhouse": Board("Anthropic", "greenhouse", "anthropic"),
        "lever": Board("Palantir", "lever", "palantir"),
        "ashby": Board("Ramp", "ashby", "ramp"),
    }
