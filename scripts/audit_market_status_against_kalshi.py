"""Read-only audit comparing local market status with live Kalshi status.

This is intentionally diagnostic only. It does not update local rows; use the
bounded lifecycle refresh job for that.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_

from app.db.models import Market, MarketMetric
from app.db.session import SessionLocal
from app.services.kalshi_rest import KalshiRestClient
from app.services.market_lifecycle import market_lifecycle


def _row_payload(market: Market, metric: MarketMetric | None) -> dict[str, Any]:
    return {
        "market_id": market.market_id,
        "title": market.title,
        "local_status": market.status,
        "local_close_time": market.close_time.isoformat()
        if market.close_time is not None
        else None,
        "volume_24h_contracts": int(metric.volume_24h_contracts)
        if metric is not None and metric.volume_24h_contracts is not None
        else None,
        "trade_count": int(metric.trade_count or 0) if metric is not None else 0,
        "latest_snapshot_ts": metric.latest_snapshot_ts.isoformat()
        if metric is not None and metric.latest_snapshot_ts is not None
        else None,
    }


def _sample_markets(
    *,
    limit: int,
    include_stale_dated: bool,
    title_contains: str | None,
) -> list[tuple[Market, MarketMetric | None]]:
    db = SessionLocal()
    try:
        rows: list[tuple[Market, MarketMetric | None]] = []
        if title_contains:
            rows.extend(
                db.query(Market, MarketMetric)
                .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
                .filter(Market.title.ilike(f"%{title_contains}%"))
                .order_by(Market.id.desc())
                .limit(limit)
                .all()
            )

        rows.extend(
            db.query(Market, MarketMetric)
            .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
            .filter(Market.status.in_(["active", "open"]))
            .filter(Market.title != Market.market_id)
            .order_by(
                MarketMetric.volume_24h_contracts.desc().nullslast(),
                MarketMetric.trade_count.desc().nullslast(),
                MarketMetric.updated_at.desc().nullslast(),
            )
            .limit(limit)
            .all()
        )

        if include_stale_dated:
            rows.extend(
                db.query(Market, MarketMetric)
                .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
                .filter(Market.status.in_(["active", "open"]))
                .filter(Market.title != Market.market_id)
                .filter(
                    or_(
                        Market.close_time <= func.now(),
                        Market.market_id.op("~")("26(APR|MAY)[0-9]{2}"),
                    )
                )
                .order_by(Market.updated_at.desc().nullslast())
                .limit(limit)
                .all()
            )

        seen: set[str] = set()
        out: list[tuple[Market, MarketMetric | None]] = []
        for market, metric in rows:
            if market.market_id in seen:
                continue
            seen.add(market.market_id)
            out.append((market, metric))
            if len(out) >= limit:
                break
        return out
    finally:
        db.close()


def audit_market_statuses(
    *,
    limit: int,
    include_stale_dated: bool,
    title_contains: str | None,
    sleep_seconds: float,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    client = KalshiRestClient(retry_attempts=2, page_sleep_sec=0.0)
    rows = _sample_markets(
        limit=limit,
        include_stale_dated=include_stale_dated,
        title_contains=title_contains,
    )
    results: list[dict[str, Any]] = []
    mismatches = 0
    upstream_finalized_local_active = 0

    for market, metric in rows:
        payload = _row_payload(market, metric)
        payload["local_lifecycle"] = market_lifecycle(market, now=now)
        try:
            upstream = client.get_market(market.market_id)
        except Exception as exc:
            payload["kalshi_error"] = repr(exc)
            results.append(payload)
            time.sleep(sleep_seconds)
            continue

        if upstream is None:
            payload["kalshi_status"] = "missing"
        else:
            payload["kalshi_status"] = upstream.get("status")
            payload["kalshi_close_time"] = upstream.get("close_time")
            payload["kalshi_result"] = upstream.get("result")
            payload["kalshi_settlement_timer_seconds"] = upstream.get(
                "settlement_timer_seconds"
            )

        local_status = str(payload.get("local_status") or "").lower()
        kalshi_status = str(payload.get("kalshi_status") or "").lower()
        if kalshi_status and local_status and kalshi_status != local_status:
            mismatches += 1
        if local_status in {"active", "open"} and kalshi_status in {
            "finalized",
            "settled",
            "closed",
        }:
            upstream_finalized_local_active += 1

        results.append(payload)
        time.sleep(sleep_seconds)

    return {
        "checked": len(results),
        "mismatches": mismatches,
        "upstream_finalized_local_active": upstream_finalized_local_active,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--include-stale-dated",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--title-contains", default=None)
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    args = parser.parse_args()

    result = audit_market_statuses(
        limit=max(1, args.limit),
        include_stale_dated=bool(args.include_stale_dated),
        title_contains=args.title_contains,
        sleep_seconds=max(0.0, args.sleep_seconds),
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
