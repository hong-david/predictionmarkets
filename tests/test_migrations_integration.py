"""Integration test: alembic migrations apply cleanly and constraints work.

Requires a real Postgres reachable via app.core.config.settings. Conftest
points POSTGRES_DB at `surveillance_test`. Enable with:

    docker compose up -d postgres
    docker compose exec -T postgres psql -U postgres \
        -c "CREATE DATABASE surveillance_test"
    RUN_INTEGRATION=1 .venv/Scripts/python.exe -m pytest tests/test_migrations_integration.py -v

The test is fully self-cleaning: it drops the public schema before upgrading,
so it does not assume a pre-existing state.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import BookEvent, Market, Trade


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Integration test; set RUN_INTEGRATION=1 (and ensure Postgres is up) to enable.",
)


def _hot_storage_decision():
    from app.services.retention import StorageDecision

    return StorageDecision(
        tier="hot",
        score=50,
        process_realtime=True,
        store_raw_hot=True,
        raw_ttl_hours=168,
        store_features=True,
        store_case_evidence=False,
        sample_rate=1.0,
        reasons=("test_override",),
    )


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(settings.database_url, future=True)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


def test_all_event_tables_exist(engine):
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    expected = {
        "markets",
        "market_snapshots",
        "anomalies",
        "trades",
        "book_events",
        "pipeline_heartbeats",
    }
    assert expected <= tables, f"missing: {expected - tables}"


def test_trades_has_unique_trade_id(engine):
    insp = inspect(engine)
    indexes = insp.get_indexes("trades")
    unique_cols = {tuple(i["column_names"]) for i in indexes if i["unique"]}
    assert ("trade_id",) in unique_cols


def test_book_events_has_session_seq_side_price_unique(engine):
    insp = inspect(engine)
    uniques = insp.get_unique_constraints("book_events")
    by_name = {u["name"]: tuple(u["column_names"]) for u in uniques}
    assert by_name.get("uq_book_events_session_seq_side_price") == (
        "session_id",
        "seq",
        "side",
        "price_dollars",
    )


def test_book_events_has_market_pk_id_composite_index(engine):
    insp = inspect(engine)
    composite_cols = {
        tuple(i["column_names"])
        for i in insp.get_indexes("book_events")
    }
    assert ("market_pk", "id") in composite_cols


def test_trade_unique_constraint_blocks_duplicates(engine):
    """The DB must reject a duplicate trade_id, regardless of ORM-level dedup."""
    with Session(engine) as db:
        market = Market(
            platform="kalshi",
            market_id=f"KXTEST-{uuid.uuid4().hex[:8]}",
            title="Integration test market",
            status="active",
        )
        db.add(market)
        db.flush()

        trade_id = f"trade-{uuid.uuid4().hex}"
        db.add(
            Trade(
                market_pk=market.id,
                trade_id=trade_id,
                ts=__import__("datetime").datetime.now(
                    tz=__import__("datetime").timezone.utc
                ),
                yes_price_dollars=Decimal("0.55"),
                count_fp=Decimal("100.00"),
                taker_side="yes",
            )
        )
        db.commit()

        db.add(
            Trade(
                market_pk=market.id,
                trade_id=trade_id,
                ts=__import__("datetime").datetime.now(
                    tz=__import__("datetime").timezone.utc
                ),
                yes_price_dollars=Decimal("0.55"),
                count_fp=Decimal("100.00"),
                taker_side="yes",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_lazy_upsert_creates_stub_market(engine):
    """A trade for a never-before-seen ticker auto-creates a stub Market row
    with `status='unknown'`, and the trade itself is then inserted normally.

    This covers the lazy-upsert path that the unit tests can only stub out:
    the round-trip INSERT ... ON CONFLICT DO NOTHING -> SELECT must actually
    materialise the new row in Postgres for the handler to keep going.

    Raw-tape gating in production may skip persisting the trade for a cold
    stub; this test is about DB idempotence, not policy — bypass the gate.
    """
    from unittest.mock import patch

    from app.db.models import Trade
    from app.services.kalshi_ws import handle_trade_message

    unique_ticker = f"KXLAZY-{uuid.uuid4().hex[:8]}"
    payload = {
        "type": "trade",
        "msg": {
            "market_ticker": unique_ticker,
            "trade_id": f"t-{uuid.uuid4().hex}",
            "ts_ms": 1_700_000_000_000,
            "yes_price_dollars": "0.42",
            "count_fp": "100.00",
            "taker_side": "yes",
        },
    }

    with patch(
        "app.services.kalshi_ws._raw_tape_decision",
        return_value=_hot_storage_decision(),
    ):
        handle_trade_message(payload)

    with Session(engine) as db:
        market = db.query(Market).filter(Market.market_id == unique_ticker).one_or_none()
        assert market is not None, "lazy upsert did not create a Market row"
        assert market.status == "unknown"
        assert market.title == unique_ticker

        n_trades = (
            db.query(Trade).filter(Trade.market_pk == market.id).count()
        )
        assert n_trades == 1


def test_lazy_upsert_is_idempotent_under_repeated_calls(engine):
    """Two trades on the same brand-new ticker must result in exactly one
    Market row, exercising the ON CONFLICT DO NOTHING branch.

    (Tape gate bypass: same as `test_lazy_upsert_creates_stub_market`.)
    """
    from unittest.mock import patch

    from app.services.kalshi_ws import handle_trade_message

    unique_ticker = f"KXLAZY-{uuid.uuid4().hex[:8]}"

    for i in range(2):
        with patch(
            "app.services.kalshi_ws._raw_tape_decision",
            return_value=_hot_storage_decision(),
        ):
            handle_trade_message(
                {
                    "type": "trade",
                    "msg": {
                        "market_ticker": unique_ticker,
                        "trade_id": f"t-{uuid.uuid4().hex}",
                        "ts_ms": 1_700_000_000_000 + i,
                        "yes_price_dollars": "0.42",
                        "count_fp": "100.00",
                        "taker_side": "yes",
                    },
                }
            )

    with Session(engine) as db:
        n_markets = (
            db.query(Market).filter(Market.market_id == unique_ticker).count()
        )
        assert n_markets == 1


def test_resolve_book_market_tickers_falls_back_when_no_snapshots(engine):
    """Cold-start path: when no snapshots have positive volume, the resolver
    falls back to `updated_at desc` so we still subscribe to *something*.

    NOTE: Module-scoped engine fixture means earlier tests can leak state.
    We explicitly clear `market_snapshots` here to assert the cold-start
    branch in isolation. Run order matters: the volume-ranking test below
    relies on this clear having happened.
    """
    from unittest.mock import patch

    from app.services import kalshi_ws

    with Session(engine) as db:
        db.execute(text("DELETE FROM market_snapshots"))
        db.commit()

    suffix = uuid.uuid4().hex[:6]
    only = Market(
        platform="kalshi",
        market_id=f"KXCOLD-{suffix}",
        title="cold",
        status="active",
    )
    with Session(engine) as db:
        db.add(only)
        db.commit()

    with patch.object(kalshi_ws.settings, "kalshi_book_market_tickers", []):
        with patch.object(kalshi_ws.settings, "kalshi_book_market_limit", 50):
            tickers = kalshi_ws.resolve_book_market_tickers()

    assert f"KXCOLD-{suffix}" in tickers


def test_resolve_book_market_tickers_ranks_by_volume(engine):
    """Volume-ranked path: the resolver picks the markets whose latest
    snapshot has the highest 24h volume, regardless of `Market.updated_at`.
    This is the smoke-test war-story scenario in test form."""
    from unittest.mock import patch

    from app.db.models import MarketSnapshot
    from app.services import kalshi_ws

    suffix = uuid.uuid4().hex[:6]
    high = Market(platform="kalshi", market_id=f"KXVOLHI-{suffix}", title="hi", status="active")
    mid = Market(platform="kalshi", market_id=f"KXVOLMD-{suffix}", title="md", status="active")
    low = Market(platform="kalshi", market_id=f"KXVOLLO-{suffix}", title="lo", status="unknown")
    dead = Market(platform="kalshi", market_id=f"KXVOLDEAD-{suffix}", title="x", status="active")

    with Session(engine) as db:
        db.add_all([high, mid, low, dead])
        db.flush()
        db.add_all(
            [
                MarketSnapshot(market_pk=high.id, volume_fp=Decimal("9999.00")),
                MarketSnapshot(market_pk=mid.id, volume_fp=Decimal("500.00")),
                MarketSnapshot(market_pk=low.id, volume_fp=Decimal("1.00")),
                MarketSnapshot(market_pk=dead.id, volume_fp=None),
            ]
        )
        db.commit()

    with patch.object(kalshi_ws.settings, "kalshi_book_market_tickers", []):
        with patch.object(kalshi_ws.settings, "kalshi_book_market_limit", 50):
            tickers = kalshi_ws.resolve_book_market_tickers()

    test_tickers = [t for t in tickers if t.endswith(suffix)]
    assert test_tickers == [
        f"KXVOLHI-{suffix}",
        f"KXVOLMD-{suffix}",
        f"KXVOLLO-{suffix}",
    ]


def test_book_event_on_conflict_do_nothing_is_idempotent(engine):
    """Re-applying the same (session_id, seq, side, price) is a no-op."""
    with Session(engine) as db:
        market = Market(
            platform="kalshi",
            market_id=f"KXTEST-{uuid.uuid4().hex[:8]}",
            title="Integration test market",
            status="active",
        )
        db.add(market)
        db.flush()

        session_id = str(uuid.uuid4())
        row = {
            "market_pk": market.id,
            "session_id": session_id,
            "seq": 1,
            "ts": None,
            "side": "yes",
            "price_dollars": Decimal("0.4200"),
            "size_fp": Decimal("100.00"),
            "delta_fp": None,
            "is_snapshot": True,
        }

        stmt = (
            pg_insert(BookEvent)
            .values(**row)
            .on_conflict_do_nothing(
                index_elements=["session_id", "seq", "side", "price_dollars"]
            )
        )
        db.execute(stmt)
        db.commit()

        # Re-applying must not raise and must not insert a second row.
        db.execute(stmt)
        db.commit()

        count = db.scalar(
            text(
                "SELECT count(*) FROM book_events "
                "WHERE session_id = :sid AND seq = :seq AND side = :side"
            ),
            {"sid": session_id, "seq": 1, "side": "yes"},
        )
        assert count == 1
