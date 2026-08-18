from pathlib import Path

import pytest

from jobhunter.archive import open_store
from jobhunter.archive.local import LocalFS


def test_put_get_exists_and_no_overwrite(tmp_path: Path) -> None:
    s = LocalFS(tmp_path)
    assert s.put("a/b.txt", b"one") is True
    assert s.put("a/b.txt", b"two") is False
    assert s.get("a/b.txt") == b"one"
    assert s.exists("a/b.txt") and not s.exists("a/c.txt")


def test_get_missing_raises_keyerror(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        LocalFS(tmp_path).get("nope")


def test_list_is_sorted_and_supports_start_after(tmp_path: Path) -> None:
    s = LocalFS(tmp_path)
    for k in ["attempts/x/2.json", "attempts/x/1.json", "attempts/y/1.json", "blobs/z"]:
        s.put(k, b"")
    assert list(s.list("attempts/")) == [
        "attempts/x/1.json", "attempts/x/2.json", "attempts/y/1.json",
    ]
    assert list(s.list("attempts/", start_after="attempts/x/1.json")) == [
        "attempts/x/2.json", "attempts/y/1.json",
    ]
    assert list(s.list("nothing/")) == []


def test_open_store_file_url(tmp_path: Path) -> None:
    s = open_store(f"file://{tmp_path}")
    assert isinstance(s, LocalFS)
    s.put("k", b"v")
    assert (tmp_path / "k").read_bytes() == b"v"


def test_open_store_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError):
        open_store("ftp://x")
