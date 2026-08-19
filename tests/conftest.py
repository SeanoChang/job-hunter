import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jobhunter.models import Board

FIXTURES = Path(__file__).parent / "fixtures"

TEST_DSN = os.environ.get(
    "JOB_HUNTER_TEST_DATABASE_URL",
    "postgresql://jobhunter:jobhunter@localhost:5432/jobhunter",
)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def boards() -> dict[str, Board]:
    return {
        "greenhouse": Board("Anthropic", "greenhouse", "anthropic"),
        "lever": Board("Palantir", "lever", "palantir"),
        "ashby": Board("Ramp", "ashby", "ramp"),
    }


@pytest.fixture
def pg() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """A connection whose search_path is a fresh schema with the DDL applied."""
    from jobhunter.store import db

    schema = f"t_{uuid.uuid4().hex[:10]}"
    try:
        conn = db.connect(TEST_DSN, schema=schema)
    except psycopg.OperationalError as e:  # pragma: no cover - environment guidance
        pytest.fail(
            f"Postgres not reachable at {TEST_DSN}: {e}\n"
            "Start it with `docker compose up -d postgres` or set JOB_HUNTER_TEST_DATABASE_URL."
        )
    db.init(conn, schema)
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        # `rebuild` builds in "<schema>_new" and leaves "<schema>_previous" behind after the
        # swap; both belong to this test and must go with it.
        for name in (schema, f"{schema}_new", f"{schema}_previous"):
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        conn.commit()
        conn.close()
