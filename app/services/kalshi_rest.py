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

from typing import Iterator

import httpx


class KalshiRestClient:
    DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    PAGE_SIZE = 1000  # Kalshi's documented max per page.

    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        self.base_url = base_url

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
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url)
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

        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, params=params)
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
