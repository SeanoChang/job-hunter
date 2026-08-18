"""s3:// backend. Works against Cloudflare R2 (AWS_ENDPOINT_URL, region 'auto') and MinIO."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from jobhunter.archive.base import ArchiveError


class S3Compatible:
    def __init__(self, bucket: str, prefix: str = "", *, client: Any | None = None) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self.region = os.environ.get("AWS_DEFAULT_REGION", "auto")
        self._client = client or boto3.client("s3", region_name=self.region)

    def _key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def _strip(self, full: str) -> str:
        return full[len(self._prefix) + 1 :] if self._prefix else full

    def put(self, key: str, data: bytes) -> bool:
        if self.exists(key):
            return False
        try:
            self._client.put_object(Bucket=self._bucket, Key=self._key(key), Body=data)
        except (BotoCoreError, ClientError) as e:
            raise ArchiveError(f"put {key}: {e}") from e
        return True

    def get(self, key: str) -> bytes:
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=self._key(key))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise KeyError(key) from e
            raise ArchiveError(f"get {key}: {e}") from e
        except BotoCoreError as e:
            raise ArchiveError(f"get {key}: {e}") from e
        return bytes(obj["Body"].read())

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(key))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise ArchiveError(f"head {key}: {e}") from e
        except BotoCoreError as e:
            raise ArchiveError(f"head {key}: {e}") from e
        return True

    def list(self, prefix: str, start_after: str | None = None) -> Iterator[str]:
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": self._key(prefix)}
        if start_after:
            kwargs["StartAfter"] = self._key(start_after)
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    yield self._strip(obj["Key"])
        except (BotoCoreError, ClientError) as e:
            raise ArchiveError(f"list {prefix}: {e}") from e
