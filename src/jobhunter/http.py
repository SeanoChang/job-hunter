"""One HTTP client for all sources: timeouts, bounded retries, size cap, honest transport
verdicts."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

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


def _retryable(exc: Exception) -> bool:
    # Every transport-level failure (timeouts, connect/read/write errors, protocol errors,
    # proxy errors) is worth another try; anything else (bad URL, redirect loop) is not.
    return isinstance(exc, httpx.TransportError) and not isinstance(exc, httpx.UnsupportedProtocol)


class Fetcher:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        retries: int = 3,
        backoff: float = 1.0,
        max_bytes: int = 64 * 2**20,
        sleep: Callable[[float], None] = time.sleep,
        spacing_ms: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client or default_client()
        self._retries = max(1, retries)  # attempts; 0 still means one request
        self._backoff = backoff
        self._max_bytes = max_bytes
        self._sleep = sleep
        self._spacing_ms = spacing_ms
        self._clock = clock
        self._last_request_at: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: Any | None = None,
    ) -> FetchResult:
        """Request url with bounded retries. The verdict always describes the LAST attempt.

        POST sends json_body as the request body with Content-Type application/json.
        Consecutive requests to the same host are spaced at least spacing_ms apart.
        """
        self._wait_for_spacing(url)
        t0 = self._clock()
        last_resp: tuple[int, bytes] | None = None
        last_exc: Exception | None = None
        for attempt in range(self._retries):
            if attempt:
                self._sleep(self._backoff * (2 ** (attempt - 1)))
            try:
                with self._client.stream(method, url, json=json_body) as resp:
                    body = self._read_capped(resp)
                    if body is None:
                        return FetchResult(resp.status_code, b"", self._clock() - t0,
                                           "too_large", f"body exceeded {self._max_bytes} bytes")
                    if 200 <= resp.status_code < 300:
                        return FetchResult(resp.status_code, body, self._clock() - t0,
                                           "ok", None)
                    last_resp, last_exc = (resp.status_code, body), None
                    if resp.status_code not in _RETRY_STATUSES:
                        break
            except httpx.HTTPError as e:
                last_resp, last_exc = None, e
                if not _retryable(e):
                    break
        elapsed = self._clock() - t0
        if last_exc is not None:
            return FetchResult(None, b"", elapsed, _classify(last_exc),
                               f"{type(last_exc).__name__}: {last_exc}")
        assert last_resp is not None  # the loop ran at least once and did not raise
        return FetchResult(last_resp[0], last_resp[1], elapsed, "http_error",
                           f"HTTP {last_resp[0]}")

    def _wait_for_spacing(self, url: str) -> None:
        if self._spacing_ms <= 0:
            return
        host = httpx.URL(url).host
        now = self._clock()
        last = self._last_request_at.get(host)
        if last is not None:
            remaining = self._spacing_ms / 1000 - (now - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at[host] = self._clock()

    def _read_capped(self, resp: httpx.Response) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
