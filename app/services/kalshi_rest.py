"""Thin client for Kalshi's REST market-data endpoints.

The Kalshi API is paginated by an opaque `cursor` returned alongside each page.
The previous implementation only fetched a single page, which silently
under-ingested the universe (we ended up with ~400 exotic combinatorial markets
instead of the open mainstream ones), so the WS feed had nothing to match.

This client exposes both a single-page `get_markets` (kept for backwards
compatibility) and an `iter_markets` generator that walks the cursor until
exhausted. The smoke test against live Kalshi was what surfaced the bug.
"""

from __future__ import annotations

import os
import time
from typing import Iterator

import httpx


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _retry_after_seconds(response: httpx.Response, fallback: float) -> float:
    raw = response.headers.get("Retry-After")
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return max(0.0, float(raw))
    except ValueError:
        return fallback


class KalshiRestClient:
    DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    PAGE_SIZE = 1000  # Kalshi's documented max per page.

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        retry_attempts: int | None = None,
        retry_backoff_sec: float | None = None,
        page_sleep_sec: float | None = None,
    ) -> None:
        self.base_url = base_url
        self.retry_attempts = max(
            1,
            retry_attempts
            if retry_attempts is not None
            else _env_int("KALSHI_REST_RETRY_MAX_ATTEMPTS", 3),
        )
        self.retry_backoff_sec = max(
            0.0,
            retry_backoff_sec
            if retry_backoff_sec is not None
            else _env_float("KALSHI_REST_RETRY_BACKOFF_SEC", 1.0),
        )
        self.page_sleep_sec = max(
            0.0,
            page_sleep_sec
            if page_sleep_sec is not None
            else _env_float("KALSHI_REST_PAGE_SLEEP_SEC", 0.0),
        )

    def _get_with_retries(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        for attempt in range(self.retry_attempts):
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, params=params)
            if response.status_code != 429:
                return response
            if attempt >= self.retry_attempts - 1:
                return response
            delay = _retry_after_seconds(
                response,
                self.retry_backoff_sec * (2**attempt),
            )
            if delay > 0:
                time.sleep(delay)

        return response

    def get_market(self, ticker: str) -> dict | None:
        """Fetch a single market by its `ticker` (the Kalshi `market_id`).

        The bulk `/markets` endpoint is filtered by `status` and paginated
        alphabetically, so it cannot drain the WS-driven lazy-upsert backlog
        — markets in `status='active'` or `'finalized'` are exactly the live
        and just-expired ones we care most about, and a `status=open` sweep
        misses them. This per-ticker endpoint is the right tool for that
        targeted hydration.

        Returns the `market` dict from the response body, or `None` if the
        ticker no longer exists upstream (404).
        """
        url = f"{self.base_url}/markets/{ticker}"
        response = self._get_with_retries(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("market")

    def get_markets(
        self,
        limit: int = 100,
        cursor: str | None = None,
        status: str | None = None,
    ) -> dict:
        """Fetch a single page of markets.

        Args:
            limit: page size; Kalshi caps at 1000.
            cursor: opaque continuation token returned by a previous call.
            status: comma-separated subset of {open, closed, settled,
                initialized, deactivated}. We default to the unfiltered set
                here; callers that want only the actively trading universe
                should pass `status="open"`.

        Returns:
            The decoded JSON body, including `markets` and `cursor`.
        """
        url = f"{self.base_url}/markets"
        params: dict[str, str | int] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status

        response = self._get_with_retries(url, params=params)
        response.raise_for_status()
        return response.json()

    def iter_markets(
        self,
        status: str | None = "open",
        limit: int = PAGE_SIZE,
        max_markets: int | None = None,
    ) -> Iterator[dict]:
        """Yield every market dict across all pages, following the cursor.

        Defaults to `status="open"` so the surveillance universe is the
        actively-trading set rather than every settled market in history.
        Pass `status=None` to disable the filter.

        `max_markets` is a defensive cap; the generator stops yielding once
        that many markets have been produced. Use it for tests or to bound
        a one-off backfill. Set to `None` for no cap.
        """
        cursor: str | None = None
        produced = 0

        while True:
            page = self.get_markets(limit=limit, cursor=cursor, status=status)
            markets = page.get("markets", [])
            if not markets:
                return

            for market in markets:
                yield market
                produced += 1
                if max_markets is not None and produced >= max_markets:
                    return

            cursor = page.get("cursor") or None
            if not cursor:
                return
            if self.page_sleep_sec > 0:
                time.sleep(self.page_sleep_sec)
