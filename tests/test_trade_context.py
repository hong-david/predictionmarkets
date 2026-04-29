from __future__ import annotations

from datetime import datetime, timezone

from app.services.trade_context import (
    MarketContext,
    build_peer_baselines,
    explain_trades_with_context,
)


def _market() -> MarketContext:
    return MarketContext(
        market_pk=1,
        market_id="KXTEST",
        category="macro",
        subcategory="cpi",
        manipulability_prior="high",
        event_id="KXEVENT",
        close_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )


def test_context_scores_liquidity_adjusted_followthrough() -> None:
    trades = [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "yes_price": 0.50,
            "count": 10,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T10:01:00+00:00",
            "yes_price": 0.51,
            "count": 8,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T10:02:00+00:00",
            "yes_price": 0.57,
            "count": 120,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T10:07:00+00:00",
            "yes_price": 0.62,
            "count": 5,
            "taker_side": "yes",
        },
    ]
    snapshots = [
        {
            "ts": "2026-01-01T10:01:30+00:00",
            "yes_bid": 0.50,
            "yes_ask": 0.58,
            "last_price": 0.51,
            "volume_24h": 400,
            "open_interest": 500,
        }
    ]

    out = explain_trades_with_context(
        trades,
        market=_market(),
        snapshots=snapshots,
    )

    flagged = out[2]
    assert flagged is not None
    assert flagged["score"] >= 3
    assert "large_size_vs_open_interest" in flagged["reasons"]
    assert "price_impact_persisted" in flagged["reasons"]
    assert flagged["features"]["size_vs_open_interest"] == 0.24
    assert flagged["features"]["spread_at_trade"] == 0.08
    assert flagged["features"]["directional_impact"] == 0.06
    assert flagged["features"]["followthrough_5m"] == 0.11


def test_peer_baseline_and_pre_news_timing_add_reasons() -> None:
    peer_rows = [
        {
            "market_pk": i % 3,
            "category": "macro",
            "subcategory": "cpi",
            "yes_price": 0.50 + (i % 4) * 0.005,
            "count": 10 + i,
        }
        for i in range(30)
    ]
    baseline = build_peer_baselines(peer_rows)[("macro", "cpi")]
    trades = [
        {
            "ts": "2026-01-01T09:00:00+00:00",
            "yes_price": 0.40,
            "count": 20,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T09:05:00+00:00",
            "yes_price": 0.48,
            "count": 500,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T09:10:00+00:00",
            "yes_price": 0.55,
            "count": 20,
            "taker_side": "yes",
        },
    ]
    news = [
        {
            "first_seen_at": "2026-01-01T11:00:00+00:00",
            "relevance_score": 0.9,
        }
    ]

    out = explain_trades_with_context(
        trades,
        market=_market(),
        peer_baseline=baseline,
        news_events=news,
    )

    flagged = out[1]
    assert flagged is not None
    assert "large_size_vs_sector_p99" in flagged["reasons"]
    assert "pre_news_directional_move" in flagged["reasons"]
    assert flagged["components"]["news_timing"] == 2.0


def test_cross_market_coherence_reason() -> None:
    trades = [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "yes_price": 0.40,
            "count": 5,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T10:01:00+00:00",
            "yes_price": 0.45,
            "count": 5,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T10:06:00+00:00",
            "yes_price": 0.49,
            "count": 5,
            "taker_side": "yes",
        },
    ]
    siblings = [
        {"market_pk": 2, "ts": "2026-01-01T10:00:00+00:00", "last_price": 0.30},
        {"market_pk": 2, "ts": "2026-01-01T10:12:00+00:00", "last_price": 0.36},
        {"market_pk": 3, "ts": "2026-01-01T10:00:00+00:00", "last_price": 0.70},
        {"market_pk": 3, "ts": "2026-01-01T10:12:00+00:00", "last_price": 0.64},
    ]

    out = explain_trades_with_context(
        trades,
        market=_market(),
        sibling_snapshots=siblings,
    )

    flagged = out[1]
    assert flagged is not None
    assert "coherent_event_move" in flagged["reasons"]


def test_low_notional_trade_context_is_capped() -> None:
    trades = [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "yes_price": 0.50,
            "count": 1,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T10:01:00+00:00",
            "yes_price": 0.80,
            "count": 1,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T10:06:00+00:00",
            "yes_price": 0.84,
            "count": 1,
            "taker_side": "yes",
        },
    ]
    local = [
        None,
        {
            "score": 9.0,
            "features": {"cluster": 0.0, "trade_dollar_amount": 0.80},
            "reasons": ["large_size_vs_recent"],
        },
        None,
    ]

    out = explain_trades_with_context(
        trades,
        market=_market(),
        local_explanations=local,
    )

    flagged = out[1]
    assert flagged is not None
    assert flagged["score"] <= 1.5
    assert "low_notional_context_cap" in flagged["reasons"]
    assert flagged["features"]["trade_dollar_amount"] == 0.8


def test_recent_linked_news_discounts_post_news_move() -> None:
    trades = [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "yes_price": 0.50,
            "count": 10,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T10:02:00+00:00",
            "yes_price": 0.57,
            "count": 120,
            "taker_side": "yes",
        },
        {
            "ts": "2026-01-01T10:07:00+00:00",
            "yes_price": 0.62,
            "count": 10,
            "taker_side": "yes",
        },
    ]
    snapshots = [
        {
            "ts": "2026-01-01T10:01:30+00:00",
            "yes_bid": 0.50,
            "yes_ask": 0.58,
            "last_price": 0.51,
            "volume_24h": 400,
            "open_interest": 500,
        }
    ]

    baseline = explain_trades_with_context(
        trades,
        market=_market(),
        snapshots=snapshots,
    )[1]
    with_news = explain_trades_with_context(
        trades,
        market=_market(),
        snapshots=snapshots,
        news_events=[
            {
                "first_seen_at": "2026-01-01T09:45:00+00:00",
                "relevance_score": 0.9,
            }
        ],
    )[1]

    assert baseline is not None and with_news is not None
    assert with_news["score"] < baseline["score"]
    assert "post_news_move_discount" in with_news["reasons"]
    assert with_news["features"]["post_news_discount_multiplier"] == 0.65
