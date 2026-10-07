"""`views.py`: the payload assembly the CLI and the MCP wrapper share.

Every test here is a parity test — call the view against the fixture corpus,
invoke the `q` verb that used to assemble the same payload inside its command
body, and demand the two `data` payloads be equal. That equality is the whole
claim of the refactor: one payload with two faces, not two payloads that drift.

The envelope serialises through `json.dumps(default=str)`, so the comparison
goes through the same conversion — a view may hand back a datetime the CLI
would have stringified, and that would be a real difference, not a formatting
one.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from jobhunter import views
from jobhunter.config import Settings
from jobhunter.timeutil import parse_iso
from tests.l2.v2.conftest import make_multi_kind_serving_record
from tests.test_cli_q import (  # noqa: F401  -- qenv is a fixture, used by name
    DAY2,
    ISO0,
    SEED_BUNDLE,
    SEED_TUPLE,
    _data,
    _doc_hash,
    _seed_profile,
    _seed_v3_profile,
    qenv,
)

NOW = DAY2 + timedelta(hours=1)  # the clock `qenv` pins the CLI to


@pytest.fixture
def corpus(qenv: Path) -> Path:  # noqa: F811  -- the imported fixture, under a free name
    """`test_cli_q`'s three ingest days, requested under a name the tests below
    do not shadow — one corpus, read through both faces."""
    return qenv


def _as_json(data: Any) -> Any:
    return json.loads(json.dumps(data, default=str))


def test_postings_view_is_what_q_postings_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    page = views.postings_view(pg)
    assert _as_json(page.data) == _data(["q", "postings"])["data"]
    assert page.truncated is False and page.next_cursor is None
    first = views.postings_view(pg, limit=2)
    body = _data(["q", "postings", "--limit", "2"])
    assert _as_json(first.data) == body["data"]
    assert first.truncated is True and first.next_cursor == body["meta"]["next_cursor"]
    # the filters reach the query, not only the shaping
    assert [r["uid"] for r in views.postings_view(pg, status="closed").rows()] == ["ab:ramp:y"]
    assert views.postings_view(pg, source="ashby", board="ramp").rows() != []
    assert views.postings_view(pg, search="rUsT").rows()[0]["uid"] == "ab:ramp:x"


def test_posting_view_matches_and_reports_a_miss_as_none(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    page = views.posting_view(pg, "ab:ramp:x")
    assert page is not None
    assert _as_json(page.data) == _data(["q", "posting", "ab:ramp:x"])["data"]
    closed = views.posting_view(pg, "ab:ramp:y")
    assert closed is not None
    assert _as_json(closed.data) == _data(["q", "posting", "ab:ramp:y"])["data"]
    assert views.posting_view(pg, "ab:ramp:nope") is None


def test_events_view_is_what_q_events_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    assert _as_json(views.events_view(pg).data) == _data(["q", "events"])["data"]
    assert _as_json(views.events_view(pg, kinds=("closed",)).data) == _data(
        ["q", "events", "--kind", "closed"])["data"]
    first = views.events_view(pg, limit=2)
    body = _data(["q", "events", "--limit", "2"])
    assert _as_json(first.data) == body["data"]
    assert first.truncated is True and first.next_cursor == body["meta"]["next_cursor"]
    after = views.events_view(pg, limit=2, after_event_id=int(first.next_cursor or 0))
    assert _as_json(after.data) == _data(
        ["q", "events", "--limit", "2", "--after", body["meta"]["next_cursor"]])["data"]
    assert views.events_view(pg, uid="ab:ramp:w").rows() != []
    assert views.events_view(pg, source="lever", board="palantir").rows() == []


def test_boards_view_is_what_q_boards_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    page = views.boards_view(pg)
    assert _as_json(page.data) == _data(["q", "boards"])["data"]
    assert page.truncated is False
    assert views.boards_view(pg, unhealthy_only=True).rows() == []


def test_document_view_slices_and_reports_a_miss_as_none(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    dh = _doc_hash()
    page = views.document_view(pg, dh)
    assert page is not None
    assert _as_json(page.data) == _data(["q", "document", dh[:12]])["data"]
    sliced = views.document_view(pg, dh, slice_="0:1")
    assert sliced is not None
    assert _as_json(sliced.data) == _data(["q", "document", dh[:12], "--slice", "0:1"])["data"]
    assert views.document_view(pg, "0" * 64) is None
    assert views.parse_slice("0:1") == (0, 1)
    assert views.parse_slice("500:") == (500, None) and views.parse_slice(":500") == (None, 500)
    for bad in ("1:x", "nope"):  # a usage error the caller renders; never a database one
        with pytest.raises(ValueError):
            views.parse_slice(bad)


def test_profile_view_matches_summary_and_full(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    dh = _doc_hash()
    profile = _seed_profile(pg, dh)
    settings = Settings.load()
    page = views.profile_view(pg, settings, dh)
    assert page is not None
    assert _as_json(page.data) == _data(["q", "profile", "--doc", dh[:12]])["data"]
    full = views.profile_view(pg, settings, dh, full=True)
    assert full is not None
    assert _as_json(full.data) == _data(["q", "profile", "--doc", dh[:12], "--full"])["data"]
    assert full.record()["profile"] == profile
    # the two reasons a profile is absent stay distinguishable: the row says which
    assert views.profile_view(pg, settings, "0" * 64) is None
    assert views.profile_row(pg, settings, "0" * 64) is None
    assert views.profile_row(pg, settings, dh) is not None


def test_profile_row_pins_the_current_engine_tuple(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.state import DerivedState
    from jobhunter.l2.transforms import VALIDATOR_VERSION
    from jobhunter.store import extraction

    # the current engine quarantined the doc; a retired prompt left a newer
    # "validated" row — the current verdict must win, not the stale profile
    dh = _doc_hash()
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION,
        state=DerivedState("quarantined", None), profile=None,
        updated_at="2026-08-27T00:00:00Z",
    )
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", prompt_version="demand-profile/v0",
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION,
        state=DerivedState("validated", None), profile={"facts": {}, "demand_profile": []},
        updated_at="2026-08-30T00:00:00Z",
    )
    pg.commit()
    settings = Settings.load()  # the default bundle: v1, the tuple seeded above
    row = views.profile_row(pg, settings, dh)
    assert row is not None
    assert row["prompt_version"] == PROMPT_VERSION and row["status"] == "quarantined"
    assert views.profile_view(pg, settings, dh) is None  # the stale profile is not served

    # a doc only ever extracted under a retired tuple still explains itself — labeled
    old = "e" * 64
    extraction.upsert_state(
        pg, document_hash=old, model="z-ai/glm-5.2:free", prompt_version="demand-profile/v0",
        schema_version=SCHEMA_VERSION, validator_version=VALIDATOR_VERSION,
        state=DerivedState("validated", None), profile={"facts": {}, "demand_profile": []},
        updated_at="2026-08-30T00:00:00Z",
    )
    pg.commit()
    hist = views.profile_row(pg, settings, old)
    assert hist is not None
    assert views.profile_payload(old, hist)["historical"] is True

    # and a current-tuple row is not so labeled
    fresh = "f" * 64
    _seed_profile(pg, fresh)
    current = views.profile_row(pg, settings, fresh)
    assert current is not None
    assert views.profile_payload(fresh, current)["historical"] is False


def test_claims_view_is_what_q_claims_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    dh = _doc_hash()
    _seed_profile(pg, dh)
    settings = Settings.load()
    page = views.claims_view(pg, settings, mention="python")
    assert _as_json(page.data) == _data(["q", "claims", "--mention", "python"])["data"]
    assert page.truncated is False
    assert views.claims_view(pg, settings, mention="Python", importance="required").rows() != []
    assert views.claims_view(pg, settings, mention="Python", importance="preferred").rows() == []
    assert views.claims_view(
        pg, settings, mention="Python", source="lever", board="palantir").rows() == []


def test_profile_view_serves_a_needs_review_row(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store refills a review row, so the view must answer from it — with
    the status on the payload, so an agent can see which tier it got."""
    dh = _doc_hash()
    profile = _seed_v3_profile(pg, dh, monkeypatch)
    settings = Settings.load()
    page = views.profile_view(pg, settings, dh)
    assert page is not None
    data = page.record()
    assert data["status"] == "needs_review"
    assert data["quality"]["sample_notes"]["splits"] == {"kind": 2}
    assert _as_json(page.data) == _data(["q", "profile", "--doc", dh[:12]])["data"]
    full = views.profile_view(pg, settings, dh, full=True)
    assert full is not None and full.record()["profile"] == profile

    # a quarantined row is still not a profile: it explains itself instead
    from jobhunter.l2.state import DerivedState
    from jobhunter.store import extraction

    prompt_version, schema_version, validator_version = SEED_TUPLE
    extraction.upsert_state(
        pg, document_hash=dh, model="z-ai/glm-5.2:free", prompt_version=prompt_version,
        schema_version=schema_version, validator_version=validator_version,
        state=DerivedState("quarantined", None), profile=profile,
        updated_at="2026-09-22T05:00:00Z",
    )
    pg.commit()
    assert views.profile_view(pg, settings, dh) is None


def test_claims_view_carries_the_rows_status_heading_and_modality(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch)
    settings = Settings.load()
    page = views.claims_view(pg, settings, mention="CPA")
    rows = page.rows()
    assert [r["mention"] for r in rows] == ["CPA"]
    assert rows[0]["extraction_status"] == "needs_review"
    assert rows[0]["section_heading"] == "Nice to have" and rows[0]["modality"] is None
    assert _as_json(page.data) == _data(["q", "claims", "--mention", "CPA"])["data"]
    degree = views.claims_view(pg, settings, mention="bachelor's degree").rows()
    assert degree[0]["section_heading"] == "Requirements"
    assert degree[0]["modality"] == "required"


def test_claims_view_reads_each_rows_own_statement_when_a_mention_spans_kinds(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One mention supporting two statements of different kinds is two rows in
    `profile_mentions`, and each row's heading and quoted modal phrase must come
    from ITS OWN statement: the row already carries the kind that says which.
    Answering from the mention alone puts one statement's words in the other's
    mouth — the misattribution schema 3 exists to prevent."""
    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch, record=make_multi_kind_serving_record())
    rows = views.claims_view(pg, Settings.load(), mention="CPA").rows()
    assert {r["area_kind"]: (r["section_heading"], r["modality"]) for r in rows} == {
        "qualification": ("Requirements", "required"),
        "responsibility": ("Nice to have", "preferred"),
    }
    assert {r["extraction_status"] for r in rows} == {"needs_review"}
    assert {r["schema_version"] for r in rows} == {SEED_TUPLE[1]}


def test_claims_view_leaves_a_legacy_row_without_the_schema_3_keys(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    """A v1 blob has no statements to read a heading off, so the two schema-3
    keys are not on the row at ALL — not present and null. Their absence is what
    tells a renderer this row's verdict column is the one to print; a null would
    be indistinguishable from a schema-3 row that has no heading to give."""
    dh = _doc_hash()
    _seed_profile(pg, dh)
    rows = views.claims_view(pg, Settings.load(), mention="python").rows()
    assert rows[0]["importance"] == "required"
    assert "section_heading" not in rows[0] and "modality" not in rows[0]
    assert rows[0]["extraction_status"] == "validated"
    assert rows[0]["schema_version"] == "1"  # the partition it was written under


def test_claims_view_holds_a_headingless_schema_3_row_apart_from_a_legacy_one(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-3 statement may sit under no heading and quote no modal phrase
    (`record.schema.json` makes both nullable). The row still carries both keys,
    set to null — present-and-null is "schema 3, nothing to say", absent is "an
    older vocabulary" — so no renderer has to fall back on the sentinel."""
    from jobhunter.l2.v2.types import NO_IMPORTANCE
    from tests.l2.v2.conftest import make_headingless_serving_record

    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch, record=make_headingless_serving_record())
    rows = views.claims_view(pg, Settings.load(), mention="CPA").rows()
    assert len(rows) == 1
    assert rows[0]["section_heading"] is None and rows[0]["modality"] is None
    assert rows[0]["schema_version"] == SEED_TUPLE[1]
    assert rows[0]["importance"] == NO_IMPORTANCE  # the sentinel, still in the column


def test_the_read_surface_scopes_to_the_selected_bundle(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine tuple in force is the SELECTED BUNDLE's, not the three v1
    module constants.

    The write path has keyed every row by the selected bundle's tuple since
    `l2/bundles.py` landed. While the read paths computed theirs from
    `l2.prompt.PROMPT_VERSION` / `l2.runner.SCHEMA_VERSION` /
    `l2.transforms.VALIDATOR_VERSION` instead, naming any other bundle made the
    whole read surface go dark on a corpus that was extracting normally — every
    row written under the new tuple, every read asking for v1's. That is the
    cutover blocker, so it is pinned at the helper all three call sites share
    and then at each of the three.
    """
    from jobhunter.l2.bundles import get_bundle
    from jobhunter.l2.prompt import PROMPT_VERSION
    from jobhunter.l2.runner import SCHEMA_VERSION
    from jobhunter.l2.transforms import VALIDATOR_VERSION

    v1 = get_bundle("v1")
    assert views.active_tuple(Settings.load()) == (
        v1.prompt_version, v1.schema_version, v1.validator_version)
    monkeypatch.setenv("JOB_HUNTER_L2_BUNDLE", SEED_BUNDLE)
    assert views.active_tuple(Settings.load()) == SEED_TUPLE
    # and the seeded partition really is one the v1 constants would have missed
    assert SEED_TUPLE != (PROMPT_VERSION, SCHEMA_VERSION, VALIDATOR_VERSION)

    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch)  # written under the v2 bundle's tuple
    settings = Settings.load()
    assert settings.l2_bundle == SEED_BUNDLE

    # q profile
    page = views.profile_view(pg, settings, dh)
    assert page is not None and page.record()["status"] == "needs_review"
    # q claims
    assert [r["document_hash"] for r in views.claims_view(pg, settings, mention="CPA").rows()] \
        == [dh]
    # pulse
    payload, _ = views.pulse_view(
        pg, settings, wm=None, since_iso=ISO0, limit=200, boards=None, now=NOW)
    inlined = [e for e in payload.record()["events"] if e.get("document_hash") == dh]
    assert inlined and all(e["extraction_status"] == "needs_review" for e in inlined)
    assert all(e["profile"] is not None for e in inlined)

    # select the other bundle and the same rows are out of this corpus: the
    # aggregate scopes them out entirely, and the profile — which deliberately
    # falls back so a document can always explain itself — says `historical`
    monkeypatch.setenv("JOB_HUNTER_L2_BUNDLE", "v1")
    v1_settings = Settings.load()
    assert views.claims_view(pg, v1_settings, mention="CPA").rows() == []
    fallback = views.profile_view(pg, v1_settings, dh)
    assert fallback is not None and fallback.record()["historical"] is True
    assert page.record()["historical"] is False  # and not under the bundle that wrote it


def test_claims_view_loads_one_blob_per_distinct_document_on_the_page(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two string columns per row are filled from the served blob, and blobs
    average tens of kilobytes: the page must cost ONE query over the distinct
    documents it is about to emit, never one per row and never a walk of the
    corpus. Both mentions of the seeded document produce rows, so a per-row
    fetch would show up here as two calls and two hashes asked for twice.
    """
    dh = _doc_hash()
    _seed_v3_profile(pg, dh, monkeypatch, record=make_multi_kind_serving_record())
    settings = Settings.load()
    calls: list[list[str]] = []
    real = views.queries.mention_contexts

    def counting(conn: Any, doc_hashes: list[str], **kw: Any) -> dict[str, Any]:
        calls.append(list(doc_hashes))
        return real(conn, doc_hashes, **kw)

    monkeypatch.setattr(views.queries, "mention_contexts", counting)
    rows = views.claims_view(pg, settings, mention="CPA", limit=1).rows()
    assert len(rows) == 1  # the page, bounded; two rows exist for this mention
    assert calls == [[dh]]  # one call, one hash, no second pass
    calls.clear()
    rows = views.claims_view(pg, settings, mention="CPA").rows()
    assert len(rows) == 2 and {r["document_hash"] for r in rows} == {dh}
    assert calls == [[dh]]  # two rows, one document, still one hash asked for
    calls.clear()
    assert views.claims_view(pg, settings, mention="nothing-demands-this").rows() == []
    assert calls == [[]]  # an empty page asks for nothing, and never the corpus


def test_pulse_view_is_what_the_pulse_command_emits(
    corpus: Path, pg: psycopg.Connection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_HUNTER_STATE_DIR", str(corpus / "state"))  # no cursor on disk yet
    settings = Settings.load()
    page, wm = views.pulse_view(
        pg, settings, wm=None, since_iso=None, limit=200, boards=None, now=NOW
    )
    body = _data(["pulse", "--peek"])
    assert _as_json(page.data) == body["data"]
    assert page.truncated is body["meta"]["truncated"]
    assert page.record()["first_run"] is True
    assert wm is not None  # the watermark the caller stores once the payload is out
    # --since bypasses the watermark the same way through both faces
    since, _ = views.pulse_view(
        pg, settings, wm=None, since_iso=parse_iso(ISO0).isoformat(), limit=200,
        boards=None, now=NOW,
    )
    assert _as_json(since.data) == _data(["pulse", "--since", ISO0])["data"]
    assert since.record()["first_run"] is False
    bounded, _ = views.pulse_view(
        pg, settings, wm=None, since_iso=parse_iso(ISO0).isoformat(), limit=3,
        boards=None, now=NOW,
    )
    assert bounded.truncated is True and len(bounded.record()["events"]) == 3
    assert "_truncated" not in bounded.record()  # the flag is a field of the page, not the payload
