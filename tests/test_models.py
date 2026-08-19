from datetime import UTC, datetime

from jobhunter.models import AttemptManifest, Board, PostingVersion


def test_board_key() -> None:
    assert Board("Anthropic", "greenhouse", "anthropic").key == "greenhouse:anthropic"


def test_posting_version_uid_uses_prefix() -> None:
    pv = PostingVersion(
        source="greenhouse", board="anthropic", source_id="5186067008",
        title="X", company="Anthropic", locations=(), workplace_type=None,
        is_remote=None, department=None, team=None, employment_type=None,
        compensation=None, url=None, apply_url=None, source_created_at=None,
        source_updated_at=None, description_html="",
    )
    assert pv.uid == "gh:anthropic:5186067008"


def _manifest() -> AttemptManifest:
    t0 = datetime(2026, 8, 18, 6, 0, 0, tzinfo=UTC)
    return AttemptManifest(
        attempt_id="attempts/greenhouse/anthropic/2026/08/18T060000Z.json",
        run_id="20260818T060000Z-abc123", source="greenhouse", board="anthropic",
        started_at=t0, finished_at=t0, url="https://x", http_status=200,
        transport="ok", blob_sha256="ab" * 32, payload_bytes=10, record_count=1,
        adapter_version="greenhouse/1", registry_revision="r" * 64,
        cli_version="0.1.0", error=None,
    )


def test_manifest_json_roundtrip_is_canonical() -> None:
    m = _manifest()
    data = m.to_json()
    assert data.startswith(b'{"adapter_version":')  # sorted keys
    assert AttemptManifest.from_json(data) == m


def test_manifest_json_uses_z_timestamps() -> None:
    assert b'"started_at":"2026-08-18T06:00:00Z"' in _manifest().to_json()
