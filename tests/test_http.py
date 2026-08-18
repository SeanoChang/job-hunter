import httpx

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
