from __future__ import annotations

from fastapi import Request

from app.core.rate_limit import InMemoryFixedWindowCounter, RateLimiter


def _request(
    path: str,
    *,
    client: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": raw_headers,
            "client": (client, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _limiter(*, now: list[int]) -> RateLimiter:
    clock = lambda: now[0]
    return RateLimiter(
        enabled=True,
        redis_url="redis://unused",
        default_limit_per_minute=2,
        expensive_limit_per_minute=1,
        health_limit_per_minute=5,
        window_seconds=60,
        counter=InMemoryFixedWindowCounter(time_func=clock),
        time_func=clock,
    )


def test_default_api_limit_blocks_after_window_quota() -> None:
    now = [0]
    limiter = _limiter(now=now)
    request = _request("/api/dashboard/markets")

    assert limiter.check_request(request).allowed is True
    second = limiter.check_request(request)
    assert second.allowed is True
    assert second.remaining == 0

    blocked = limiter.check_request(request)
    assert blocked.allowed is False
    assert blocked.rule.name == "api_default"
    assert blocked.reset_seconds == 60

    now[0] = 61
    assert limiter.check_request(request).allowed is True


def test_expensive_api_paths_have_separate_lower_limit() -> None:
    now = [0]
    limiter = _limiter(now=now)

    assert limiter.check_request(_request("/api/dashboard/search")).allowed is True
    blocked = limiter.check_request(_request("/api/dashboard/search"))
    assert blocked.allowed is False
    assert blocked.rule.name == "api_expensive"

    default = limiter.check_request(_request("/api/dashboard/markets"))
    assert default.allowed is True
    assert default.rule.name == "api_default"


def test_non_api_paths_are_not_limited() -> None:
    limiter = _limiter(now=[0])

    assert limiter.check_request(_request("/assets/index.js")) is None
    assert limiter.check_request(_request("/markets")) is None


def test_forwarded_ip_is_used_as_client_identifier() -> None:
    now = [0]
    limiter = _limiter(now=now)
    headers = {"x-forwarded-for": "203.0.113.7, 10.0.0.2"}

    first = limiter.check_request(
        _request("/api/dashboard/search", client="10.0.0.10", headers=headers)
    )
    second = limiter.check_request(
        _request("/api/dashboard/search", client="10.0.0.11", headers=headers)
    )
    other_ip = limiter.check_request(
        _request(
            "/api/dashboard/search",
            client="10.0.0.11",
            headers={"x-forwarded-for": "203.0.113.8"},
        )
    )

    assert first.allowed is True
    assert second.allowed is False
    assert other_ip.allowed is True
