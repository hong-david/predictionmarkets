"""Tests for KalshiRestClient pagination + filtering.

The single-page bug is exactly the kind of thing that's silent in unit-mocked
tests but fatal in production. So these tests exercise:

  - cursor follow-through across multiple pages,
  - termination when `cursor` is empty,
  - termination when `markets` is empty (regardless of cursor),
  - the `max_markets` cap,
  - that `status` is forwarded as a query param.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.services.kalshi_rest import KalshiRestClient


class _FakeRestClient(KalshiRestClient):
    """Stub that returns scripted pages instead of hitting Kalshi.

    Pages are a list of dicts in the shape of a real Kalshi response. We also
    capture the params passed to each `get_markets` call so tests can assert
    that `cursor` and `status` were forwarded correctly.
    """

    def __init__(self, pages: list[dict]) -> None:
        super().__init__()
        self._pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    def get_markets(
        self,
        limit: int = 100,
        cursor: str | None = None,
        status: str | None = None,
    ) -> dict:
        self.calls.append({"limit": limit, "cursor": cursor, "status": status})
        if not self._pages:
            return {"markets": [], "cursor": ""}
        return self._pages.pop(0)


def _market(ticker: str) -> dict:
    return {"ticker": ticker, "title": f"Title for {ticker}", "status": "open"}


def test_iter_markets_follows_cursor_across_pages():
    client = _FakeRestClient(
        pages=[
            {"markets": [_market("A"), _market("B")], "cursor": "page2"},
            {"markets": [_market("C")], "cursor": "page3"},
            {"markets": [_market("D"), _market("E")], "cursor": ""},
        ]
    )

    tickers = [m["ticker"] for m in client.iter_markets(status="open")]

    assert tickers == ["A", "B", "C", "D", "E"]
    assert [c["cursor"] for c in client.calls] == [None, "page2", "page3"]


def test_iter_markets_stops_when_cursor_is_empty():
    client = _FakeRestClient(
        pages=[
            {"markets": [_market("A")], "cursor": ""},
        ]
    )
    tickers = [m["ticker"] for m in client.iter_markets()]
    assert tickers == ["A"]
    assert len(client.calls) == 1


def test_iter_markets_stops_when_page_has_no_markets():
    """A real-world failure mode: cursor stays non-empty but markets is []."""
    client = _FakeRestClient(
        pages=[
            {"markets": [_market("A")], "cursor": "page2"},
            {"markets": [], "cursor": "page3"},
        ]
    )
    tickers = [m["ticker"] for m in client.iter_markets()]
    assert tickers == ["A"]
    assert len(client.calls) == 2


def test_iter_markets_respects_max_markets_cap():
    pages = [
        {"markets": [_market(f"M{i}") for i in range(100)], "cursor": "next"},
        {"markets": [_market(f"N{i}") for i in range(100)], "cursor": "next2"},
    ]
    client = _FakeRestClient(pages=pages)

    yielded = list(client.iter_markets(max_markets=150))
    assert len(yielded) == 150
    assert yielded[0]["ticker"] == "M0"
    assert yielded[-1]["ticker"] == "N49"
    # Should not have fetched a third page since the cap was hit.
    assert len(client.calls) == 2


def test_iter_markets_forwards_status_to_each_page_call():
    client = _FakeRestClient(
        pages=[
            {"markets": [_market("A")], "cursor": "next"},
            {"markets": [_market("B")], "cursor": ""},
        ]
    )

    list(client.iter_markets(status="open"))

    assert all(call["status"] == "open" for call in client.calls)


def test_iter_markets_passes_status_none_when_disabled():
    client = _FakeRestClient(
        pages=[{"markets": [_market("A")], "cursor": ""}],
    )
    list(client.iter_markets(status=None))
    assert client.calls[0]["status"] is None


@pytest.mark.parametrize("limit", [50, 250, 1000])
def test_iter_markets_uses_caller_supplied_page_size(limit):
    client = _FakeRestClient(
        pages=[{"markets": [_market("A")], "cursor": ""}],
    )
    list(client.iter_markets(limit=limit))
    assert client.calls[0]["limit"] == limit


class _StubHTTPClient:
    """Drop-in for `httpx.Client(...)` that returns a scripted response.

    Lets us hit `KalshiRestClient.get_market` without the network. We mimic
    httpx's context-manager + Response shape just enough.
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url: str, params=None):
        self._url = url
        self._params = params
        return self._response


class _SequenceHTTPClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url: str, params=None):
        self.calls.append({"url": url, "params": params})
        if not self._responses:
            raise AssertionError("No scripted HTTP response left")
        return self._responses.pop(0)


def _resp(status_code: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=body if body is not None else {},
        request=httpx.Request("GET", "http://stub/"),
    )


def test_get_market_returns_market_dict_on_200():
    body = {"market": {"ticker": "KXBTC15M", "title": "BTC up?", "status": "active"}}
    stub = _StubHTTPClient(_resp(200, body))
    with patch("app.services.kalshi_rest.httpx.Client", return_value=stub):
        client = KalshiRestClient()
        result = client.get_market("KXBTC15M")
    assert result == body["market"]


def test_get_market_returns_none_on_404():
    stub = _StubHTTPClient(_resp(404, {"error": "not found"}))
    with patch("app.services.kalshi_rest.httpx.Client", return_value=stub):
        client = KalshiRestClient()
        result = client.get_market("KXDOESNOTEXIST")
    assert result is None


def test_get_market_raises_on_5xx():
    stub = _StubHTTPClient(_resp(500))
    with patch("app.services.kalshi_rest.httpx.Client", return_value=stub):
        client = KalshiRestClient()
        with pytest.raises(httpx.HTTPStatusError):
            client.get_market("KXBOOM")


def test_get_markets_retries_429_before_returning_page():
    stub = _SequenceHTTPClient(
        [
            _resp(429, {"error": "rate limited"}),
            _resp(200, {"markets": [_market("A")], "cursor": ""}),
        ]
    )
    with (
        patch("app.services.kalshi_rest.httpx.Client", return_value=stub),
        patch("app.services.kalshi_rest.time.sleep") as sleep_mock,
    ):
        client = KalshiRestClient(retry_attempts=2, retry_backoff_sec=0)
        result = client.get_markets(limit=1000, status="open")

    assert result["markets"][0]["ticker"] == "A"
    assert len(stub.calls) == 2
    sleep_mock.assert_not_called()


def test_get_markets_raises_after_429_retries_are_exhausted():
    stub = _SequenceHTTPClient(
        [
            _resp(429, {"error": "rate limited"}),
            _resp(429, {"error": "still limited"}),
        ]
    )
    with (
        patch("app.services.kalshi_rest.httpx.Client", return_value=stub),
        patch("app.services.kalshi_rest.time.sleep"),
    ):
        client = KalshiRestClient(retry_attempts=2, retry_backoff_sec=0)
        with pytest.raises(httpx.HTTPStatusError):
            client.get_markets(limit=1000, status="open")

    assert len(stub.calls) == 2
