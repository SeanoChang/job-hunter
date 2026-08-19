from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from jobhunter.archive.local import LocalFS
from jobhunter.models import Board
from jobhunter.store.lifecycle import Ingestor
from jobhunter.store.queries import board_health, events_since, open_counts, panel_rows
from tests.store.helpers import ab_record, board_payload, make_manifest, write_registry


def test_queries(tmp_path: Path, pg: psycopg.Connection[dict[str, Any]]) -> None:
    store = LocalFS(tmp_path)
    rev = write_registry(store, [Board("Ramp", "ashby", "ramp")])
    t0 = datetime(2026, 8, 18, 6, tzinfo=UTC)
    ing = Ingestor(pg, store)
    ing.ingest(make_manifest(
        store, "ashby", "ramp", t0,
        board_payload("ashby", [ab_record("x", "T", "<p>t</p>"), ab_record("y", "U", "<p>u</p>")]),
        registry_revision=rev,
    ))
    ing.ingest(make_manifest(
        store, "ashby", "ramp", t0 + timedelta(days=1),
        board_payload("ashby", [ab_record("x", "T2", "<p>t</p>")]),
        registry_revision=rev,
    ))
    pg.commit()
    ev = events_since(pg, t0 + timedelta(hours=1))
    assert [(e["kind"], e["uid"]) for e in ev] == [
        ("changed", "ab:ramp:x"), ("closed", "ab:ramp:y"),
    ]
    assert ev[0]["title"] == "T2" and ev[0]["url"].endswith("/x")
    assert ev[1]["closed_lower_at"] == t0 and ev[1]["closed_upper_at"] == t0 + timedelta(days=1)
    assert panel_rows(pg)[0]["board"] == "ramp"
    h = board_health(pg)["ashby:ramp"]
    assert h["health"] == "ok" and h["observed_count"] == 1
    assert open_counts(pg) == {"ashby:ramp": 1}
