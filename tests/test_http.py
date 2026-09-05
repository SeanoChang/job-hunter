import httpx
import pytest

from jobhunter.http import USER_AGENT, Fetcher


def _client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT})


def test_ok_returns_body_and_status() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        assert req.headers["user-agent"].startswith("job-hunter/")
        return httpx.Response(200, content=b'{"jobs":[]}')

    r = Fetcher(_client(h), sleep=lambda s: None).fetch("https://x/y")
    assert r.transport == "ok" and r.status == 200 and r.body == b'{"jobs":[]}'


def test_retries_on_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, content=b"ok")

    slept: list[float] = []
    r = Fetcher(_client(h), retries=3, backoff=0.5, sleep=slept.append).fetch("https://x")
    assert r.transport == "ok" and calls["n"] == 3
    assert slept == [0.5, 1.0]


def test_http_error_after_retries_exhausted() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    r = Fetcher(_client(h), retries=3, sleep=lambda s: None).fetch("https://x")
    assert r.transport == "http_error" and r.status == 500 and r.body == b"boom"


def test_404_is_not_retried() -> None:
    calls = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    r = Fetcher(_client(h), sleep=lambda s: None).fetch("https://x")
    assert r.transport == "http_error" and r.status == 404 and calls["n"] == 1


def test_timeout_maps_to_transport_timeout() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    r = Fetcher(_client(h), retries=2, sleep=lambda s: None).fetch("https://x")
    assert r.transport == "timeout" and r.status is None and r.error


def test_connect_error_maps_to_dns_when_resolution_fails() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided", request=req)

    r = Fetcher(_client(h), retries=1, sleep=lambda s: None).fetch("https://x")
    assert r.transport == "dns"


def test_too_large_body_is_rejected() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    r = Fetcher(_client(h), max_bytes=1024, sleep=lambda s: None).fetch("https://x")
    assert r.transport == "too_large" and r.body == b""


def test_retries_zero_still_makes_one_attempt() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    r = Fetcher(_client(h), retries=0, sleep=lambda s: None).fetch("https://x")
    assert r.transport == "ok"


@pytest.mark.parametrize(
    "exc_type", [httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError]
)
def test_transport_errors_are_retried(exc_type: type[httpx.TransportError]) -> None:
    calls = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise exc_type("flaky", request=req)
        return httpx.Response(200, content=b"ok")

    r = Fetcher(_client(h), retries=3, sleep=lambda s: None).fetch("https://x")
    assert r.transport == "ok" and calls["n"] == 3


def test_verdict_reflects_last_attempt_when_exception_precedes_response() -> None:
    calls = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(503, content=b"unavailable")

    r = Fetcher(_client(h), retries=2, sleep=lambda s: None).fetch("https://x")
    assert r.transport == "http_error" and r.status == 503 and r.body == b"unavailable"


def test_non_transport_http_error_is_not_retried() -> None:
    calls = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TooManyRedirects("loop", request=req)

    r = Fetcher(_client(h), retries=3, sleep=lambda s: None).fetch("https://x")
    assert r.transport == "other" and calls["n"] == 1


def test_post_sends_json_body_and_content_type() -> None:
    seen: dict[str, object] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["content_type"] = req.headers.get("content-type")
        seen["body"] = req.content
        return httpx.Response(200, content=b'{"ok":true}')

    r = Fetcher(_client(h), sleep=lambda s: None).fetch(
        "https://x/y", method="POST", json_body={"a": 1}
    )
    assert r.transport == "ok" and r.status == 200 and r.body == b'{"ok":true}'
    assert seen["method"] == "POST"
    assert seen["content_type"] == "application/json"
    assert seen["body"] == b'{"a":1}'


def test_post_retries_on_5xx_like_get() -> None:
    calls = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 2 else httpx.Response(200, content=b"ok")

    r = Fetcher(_client(h), retries=3, sleep=lambda s: None).fetch(
        "https://x", method="POST", json_body={"q": "jobs"}
    )
    assert r.transport == "ok" and calls["n"] == 2


def test_get_without_json_body_is_unaffected_by_post_support() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert req.headers.get("content-type") is None
        assert req.content == b""
        return httpx.Response(200, content=b"ok")

    r = Fetcher(_client(h), sleep=lambda s: None).fetch("https://x/y")
    assert r.transport == "ok" and r.body == b"ok"


def test_spacing_delays_consecutive_requests_to_same_host() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    now = {"t": 0.0}
    slept: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    f = Fetcher(_client(h), spacing_ms=250, sleep=sleep, clock=clock)
    f.fetch("https://host-a/1")
    now["t"] += 0.05  # only 50ms elapsed before the next request
    f.fetch("https://host-a/2")

    assert slept == [pytest.approx(0.2)]


def test_spacing_does_not_delay_different_hosts() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    now = {"t": 0.0}
    slept: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    f = Fetcher(_client(h), spacing_ms=250, sleep=sleep, clock=clock)
    f.fetch("https://host-a/1")
    f.fetch("https://host-b/1")

    assert slept == []


def test_spacing_defaults_to_zero_and_does_not_delay() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    slept: list[float] = []
    f = Fetcher(_client(h), sleep=slept.append)
    f.fetch("https://host-a/1")
    f.fetch("https://host-a/2")

    assert slept == []


def test_spacing_allows_second_request_once_interval_has_elapsed() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    now = {"t": 0.0}
    slept: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    f = Fetcher(_client(h), spacing_ms=250, sleep=sleep, clock=clock)
    f.fetch("https://host-a/1")
    now["t"] += 0.3  # already past the 250ms spacing window
    f.fetch("https://host-a/2")

    assert slept == []
