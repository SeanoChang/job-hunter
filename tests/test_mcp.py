"""The hosted MCP server: the bearer gate, the seven read tools, and parity.

The server is the CLI's read surface with a different face, so the tests are
the same two claims made twice: nothing but `/healthz` answers without the
token, and every tool hands back exactly what `views.py` hands the CLI. The
transport is real — a Starlette `TestClient` speaks streamable HTTP JSON-RPC
into the app in-process (it is httpx over ASGI, plus the lifespan the session
manager needs) — against the same three-day Postgres corpus `tests/test_cli_q`
ingests.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from starlette.testclient import TestClient

from jobhunter import mcp, views
from jobhunter.config import Settings
from jobhunter.markdown import NORMALIZER_VERSION
from jobhunter.store import queries
from jobhunter.timeutil import parse_iso
from tests.test_cli_q import (  # noqa: F401  -- qenv is a fixture, used by name
    ISO1,
    _doc_hash,
    _seed_profile,
    qenv,
)

TOKEN = "s3cr3t-bearer"
READ_TOOLS = {"postings", "posting", "events", "document", "profile", "claims", "boards"}


@pytest.fixture
def client(
    qenv: Path,  # noqa: F811  -- the imported fixture: three ingest days on one board
    pg: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """The server, serving the fixture corpus out of the test's throwaway schema."""
    row = pg.execute("SELECT current_schema() AS s").fetchone()
    assert row is not None
    monkeypatch.setattr(mcp, "_schema", str(row["s"]))
    with TestClient(mcp.build_app(TOKEN)) as c:
        yield c


def _rpc(
    client: TestClient, method: str, params: Any = None, *, token: str | None = TOKEN
) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        mcp.MCP_PATH, headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def _result(client: TestClient, method: str, params: Any = None) -> Any:
    r = _rpc(client, method, params)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _call(client: TestClient, name: str, **arguments: Any) -> Any:
    """One tool call, its structured payload — and a failure surfaces as one."""
    result = _result(client, "tools/call", {"name": name, "arguments": arguments})
    assert result["isError"] is False, result["content"][0]["text"]
    return result["structuredContent"]


def _tool_error(client: TestClient, name: str, **arguments: Any) -> str:
    result = _result(client, "tools/call", {"name": name, "arguments": arguments})
    assert result["isError"] is True, result
    return str(result["content"][0]["text"])


def _as_json(data: Any) -> Any:
    """The view payload as it survives the wire, so a stray datetime is a real
    difference and not a formatting one (same conversion as `test_views`)."""
    return json.loads(json.dumps(data, default=str))


def test_healthz_is_the_only_open_route(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["version"] == mcp.__version__  # build version, no corpus data
    assert "data" not in r.json()
    for token in (None, "wrong", ""):
        denied = _rpc(client, "tools/list", token=token)
        assert denied.status_code == 401, denied.text
        assert denied.json() == {"error": "unauthorized"}
    malformed = client.post(
        mcp.MCP_PATH, headers={"Authorization": TOKEN, "Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert malformed.status_code == 401  # the scheme is part of the credential


def test_tools_list_names_the_whole_read_surface(client: TestClient) -> None:
    names = {t["name"] for t in _result(client, "tools/list")["tools"]}
    assert names >= READ_TOOLS
    assert len(names) >= 7


def test_postings_tool_is_the_postings_view(
    client: TestClient, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    page = _call(client, "postings")
    assert page["data"] == _as_json(views.postings_view(pg).data)
    assert page["truncated"] is False and page["next_cursor"] is None
    first = _call(client, "postings", limit=2)
    view = views.postings_view(pg, limit=2)
    assert first["data"] == _as_json(view.data)
    assert first["truncated"] is True and first["next_cursor"] == view.next_cursor
    second = _call(client, "postings", limit=2, after=first["next_cursor"])
    assert second["data"] == _as_json(
        views.postings_view(pg, limit=2, after=view.next_cursor).data)
    # the filters reach the query the way the payload prints them back
    assert [r["uid"] for r in _call(client, "postings", status="closed")["data"]] == ["ab:ramp:y"]
    assert _call(client, "postings", board="ashby:ramp")["data"] != []
    assert _call(client, "postings", search="rUsT")["data"][0]["uid"] == "ab:ramp:x"


def test_limit_is_clamped_to_the_hard_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = queries.postings_page

    def spy(conn: Any, **kw: Any) -> Any:
        seen.update(kw)
        return real(conn, **kw)

    monkeypatch.setattr(queries, "postings_page", spy)
    _call(client, "postings", limit=9999)
    assert seen["limit"] == 500


def test_the_detail_and_event_tools_are_their_views(
    client: TestClient, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    posting = views.posting_view(pg, "ab:ramp:x")
    assert posting is not None
    assert _call(client, "posting", uid="ab:ramp:x")["data"] == _as_json(posting.data)
    assert _call(client, "events")["data"] == _as_json(views.events_view(pg).data)
    assert _call(client, "events", kind="closed")["data"] == _as_json(
        views.events_view(pg, kinds=("closed",)).data)
    since = _call(client, "events", since=ISO1)
    assert since["data"] == _as_json(views.events_view(pg, since=parse_iso(ISO1)).data)
    assert _call(client, "events", since="9999d")["data"] == _as_json(views.events_view(pg).data)
    assert _call(client, "events", since="1m")["data"] == []
    page = _call(client, "events", limit=2)
    view = views.events_view(pg, limit=2)
    assert page["truncated"] is True and page["next_cursor"] == view.next_cursor
    assert _call(client, "events", limit=2, after=page["next_cursor"])["data"] == _as_json(
        views.events_view(pg, limit=2, after_event_id=int(view.next_cursor or 0)).data)
    assert _call(client, "boards")["data"] == _as_json(views.boards_view(pg).data)
    assert _call(client, "boards", unhealthy_only=True)["data"] == []


def test_the_document_and_profile_tools_are_their_views(
    client: TestClient, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    dh = _doc_hash()
    profile = _seed_profile(pg, dh)
    document = views.document_view(pg, dh)
    assert document is not None
    assert _call(client, "document", document_hash=dh)["data"] == _as_json(document.data)
    assert _call(client, "document", document_hash=dh, slice="0:1")["data"]["markdown"] == (
        document.record()["markdown"][:1])
    summary = views.profile_view(pg, dh)
    assert summary is not None
    assert _call(client, "profile", document_hash=dh)["data"] == _as_json(summary.data)
    full = _call(client, "profile", document_hash=dh, full=True)["data"]
    assert full["profile"] == profile  # quotes and spans are what `full` buys
    claims = views.claims_view(pg, Settings.load(), mention="python")
    assert _call(client, "claims", mention="python")["data"] == _as_json(claims.data)
    assert _call(client, "claims", mention="python", importance="preferred")["data"] == []


def test_absent_identifiers_and_bad_flags_are_tool_errors(
    client: TestClient, pg: psycopg.Connection[dict[str, Any]]
) -> None:
    assert "no posting 'ab:ramp:nope'" in _tool_error(client, "posting", uid="ab:ramp:nope")
    missing = _tool_error(client, "document", document_hash="0" * 64)
    assert "no document 000000000000" in missing and NORMALIZER_VERSION in missing
    assert "no extraction for" in _tool_error(client, "profile", document_hash="0" * 64)
    dh = _doc_hash()
    assert "extract run" in _tool_error(client, "profile", document_hash=dh)
    assert "open or closed" in _tool_error(client, "postings", status="bogus")
    assert "source:board" in _tool_error(client, "postings", board="ramp")
    assert "not a cursor" in _tool_error(client, "postings", after="nonsense")
    assert "not a cursor" in _tool_error(client, "events", after="nonsense")
    assert "unknown event kind" in _tool_error(client, "events", kind="exploded")
    assert "importance" in _tool_error(client, "claims", mention="python", importance="vital")
    assert "S:E" in _tool_error(client, "document", document_hash=dh, slice="nope")
    assert "since" in _tool_error(client, "events", since="last tuesday")
