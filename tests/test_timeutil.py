from datetime import UTC, datetime

from jobhunter.timeutil import from_epoch_ms, iso, parse_iso, utcnow


def test_utcnow_is_aware_utc_without_microseconds() -> None:
    now = utcnow()
    assert now.tzinfo is UTC
    assert now.microsecond == 0


def test_iso_roundtrip_z() -> None:
    dt = datetime(2026, 8, 18, 6, 1, 2, tzinfo=UTC)
    assert iso(dt) == "2026-08-18T06:01:02Z"
    assert parse_iso("2026-08-18T06:01:02Z") == dt


def test_parse_iso_offset_normalises_to_utc() -> None:
    dt = parse_iso("2026-08-03T18:25:22-04:00")
    assert dt == datetime(2026, 8, 3, 22, 25, 22, tzinfo=UTC)


def test_from_epoch_ms() -> None:
    assert from_epoch_ms(1711403416463) == datetime(
        2024, 3, 25, 21, 50, 16, 463000, tzinfo=UTC
    )
