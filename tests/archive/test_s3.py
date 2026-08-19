import os
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from jobhunter.archive import open_store
from jobhunter.archive.s3 import S3Compatible


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)


@pytest.fixture
def store() -> Iterator[S3Compatible]:
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="jh-archive")
        yield S3Compatible(bucket="jh-archive", prefix="corpus")


def test_put_is_write_once(store: S3Compatible) -> None:
    assert store.put("a/b", b"1") is True
    assert store.put("a/b", b"2") is False
    assert store.get("a/b") == b"1"


def test_prefix_is_applied(store: S3Compatible) -> None:
    store.put("k", b"v")
    obj = boto3.client("s3").get_object(Bucket="jh-archive", Key="corpus/k")
    assert obj["Body"].read() == b"v"


def test_exists_and_missing_get(store: S3Compatible) -> None:
    assert not store.exists("zzz")
    with pytest.raises(KeyError):
        store.get("zzz")


def test_list_sorted_with_start_after(store: S3Compatible) -> None:
    for k in ["attempts/b/1", "attempts/a/2", "attempts/a/1", "blobs/x"]:
        store.put(k, b"")
    assert list(store.list("attempts/")) == ["attempts/a/1", "attempts/a/2", "attempts/b/1"]
    assert list(store.list("attempts/", start_after="attempts/a/2")) == ["attempts/b/1"]


def test_open_store_s3_url() -> None:
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="jh-archive")
        s = open_store("s3://jh-archive/some/prefix")
        assert isinstance(s, S3Compatible)
        s.put("k", b"v")
        obj = boto3.client("s3").get_object(Bucket="jh-archive", Key="some/prefix/k")
        assert obj["Body"].read() == b"v"


def test_region_defaults_to_auto_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with mock_aws():
        s = S3Compatible(bucket="jh-archive")
        assert s.region == "auto"
    assert "AWS_DEFAULT_REGION" not in os.environ
