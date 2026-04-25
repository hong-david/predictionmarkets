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
    expected = {"markets", "market_snapshots", "anomalies", "trades", "book_events"}
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
