"""One HTTP client for all sources: timeouts, bounded retries, size cap,
honest transport verdicts."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from jobhunter import __version__
from jobhunter.models import FetchResult

USER_AGENT = f"job-hunter/{__version__} (+https://github.com/SeanoChang/job-hunter)"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def default_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def _classify(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc).lower()
        if any(k in msg for k in ("nodename", "name or service", "getaddrinfo", "resolve")):
            return "dns"
        if "ssl" in msg or "certificate" in msg or "tls" in msg:
            return "tls"
        return "connect"
    return "other"


class Fetcher:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        retries: int = 3,
        backoff: float = 1.0,
        max_bytes: int = 64 * 2**20,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or default_client()
        self._retries = max(1, retries)  # attempts; 0 still means one request
        self._backoff = backoff
        self._max_bytes = max_bytes
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str) -> FetchResult:
        t0 = time.monotonic()
        last_exc: Exception | None = None
        last_resp: tuple[int, bytes] | None = None
        for attempt in range(self._retries):
            if attempt:
                self._sleep(self._backoff * (2 ** (attempt - 1)))
            try:
                with self._client.stream("GET", url) as resp:
                    body = self._read_capped(resp)
                    if body is None:
                        return FetchResult(resp.status_code, b"", time.monotonic() - t0,
                                           "too_large", f"body exceeded {self._max_bytes} bytes")
                    if 200 <= resp.status_code < 300:
                        return FetchResult(resp.status_code, body, time.monotonic() - t0,
                                           "ok", None)
                    last_resp = (resp.status_code, body)
                    if resp.status_code not in _RETRY_STATUSES:
                        break
            except httpx.HTTPError as e:
                last_exc = e
                if _classify(e) == "other":
                    break
        elapsed = time.monotonic() - t0
        if last_resp is not None and (last_exc is None or last_resp[0] not in _RETRY_STATUSES):
            return FetchResult(last_resp[0], last_resp[1], elapsed, "http_error",
                               f"HTTP {last_resp[0]}")
        if last_exc is not None:
            return FetchResult(None, b"", elapsed, _classify(last_exc),
                               f"{type(last_exc).__name__}: {last_exc}")
        assert last_resp is not None
        return FetchResult(last_resp[0], last_resp[1], elapsed, "http_error",
                           f"HTTP {last_resp[0]}")

    def _read_capped(self, resp: httpx.Response) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
