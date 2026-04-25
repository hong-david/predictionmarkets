"""Pure-unit tests for the Kalshi WS message handlers.

These tests exercise only branches that do NOT need a real database — the
malformed-payload guards and the snapshot row-expansion shape. End-to-end
DB behaviour (idempotency, ON CONFLICT DO NOTHING) is covered by the
integration test that runs against a real Postgres in docker-compose.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from app.services import kalshi_ws


# ---------- helpers --------------------------------------------------------


class _FakeSession:
    """Minimal stand-in for a SQLAlchemy Session.

    Captures the statement/values passed to `db.execute()` so we can assert on
    what would have been written, without spinning up a real engine.
    """

    def __init__(self, market_lookup_result: Any = None) -> None:
        self.market_lookup_result = market_lookup_result
        self.executed_stmts: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def one_or_none(self):
        return self.market_lookup_result

    def execute(self, stmt):
        self.executed_stmts.append(stmt)

        class _Result:
            rowcount = 1

        return _Result()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _FakeMarket:
    def __init__(self, pk: int = 1, market_id: str = "KXTEST-25") -> None:
        self.id = pk
        self.market_id = market_id


class _RecordingInsert:
    """Stand-in for `pg_insert` that captures the rows passed into `.values()`.

    Avoids parsing compiled SQL (which is fragile across SQLAlchemy versions)
    by intercepting at the boundary the handler actually crosses.
    """

    def __init__(self) -> None:
        self.captured_rows: list[dict] = []

    def __call__(self, _table):
        recorder = self

        class _Stmt:
            def values(self, *args, **kwargs):
                if args and isinstance(args[0], list):
                    recorder.captured_rows.extend(args[0])
                elif args and isinstance(args[0], dict):
                    recorder.captured_rows.append(args[0])
                elif kwargs:
                    recorder.captured_rows.append(kwargs)
                return self

            def on_conflict_do_nothing(self, **_kwargs):
                return self

        return _Stmt()


# ---------- ts conversion --------------------------------------------------


def test_ts_from_ms_returns_none_for_none():
    assert kalshi_ws._ts_from_ms(None) is None


def test_ts_from_ms_converts_milliseconds_to_utc_datetime():
    out = kalshi_ws._ts_from_ms(1_700_000_000_000)
    assert out is not None
    assert out.tzinfo is timezone.utc
    assert out == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)


# ---------- trade handler --------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        {"trade_id": "t1", "ts_ms": 1},  # missing market_ticker
        {"market_ticker": "K", "ts_ms": 1},  # missing trade_id
        {"market_ticker": "K", "trade_id": "t1"},  # missing ts_ms
    ],
)
def test_handle_trade_message_skips_malformed_payloads(msg):
    fake = _FakeSession()
    with patch.object(kalshi_ws, "SessionLocal", return_value=fake):
        kalshi_ws.handle_trade_message({"type": "trade", "msg": msg})
    # Malformed payloads must not even open a session.
    assert fake.executed_stmts == []
    assert fake.commits == 0


def test_handle_trade_message_skips_unknown_market():
    fake = _FakeSession(market_lookup_result=None)
    payload = {
        "type": "trade",
        "msg": {
            "market_ticker": "DOES-NOT-EXIST",
            "trade_id": "t1",
            "ts_ms": 1_700_000_000_000,
        },
    }
    with patch.object(kalshi_ws, "SessionLocal", return_value=fake):
        kalshi_ws.handle_trade_message(payload)
    assert fake.executed_stmts == []
    assert fake.closed is True


# ---------- orderbook_delta handler ----------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        {"seq": 1, "side": "yes", "price_dollars": "0.5", "delta_fp": "1"},  # no market_ticker
        {"market_ticker": "K", "side": "yes", "price_dollars": "0.5", "delta_fp": "1"},  # no seq
        {"market_ticker": "K", "seq": 1, "price_dollars": "0.5", "delta_fp": "1"},  # no side
        {"market_ticker": "K", "seq": 1, "side": "yes", "delta_fp": "1"},  # no price
        {"market_ticker": "K", "seq": 1, "side": "yes", "price_dollars": "0.5"},  # no delta
    ],
)
def test_handle_orderbook_delta_skips_malformed_payloads(msg):
    fake = _FakeSession()
    seq = msg.pop("seq", None)
    with patch.object(kalshi_ws, "SessionLocal", return_value=fake):
        kalshi_ws.handle_orderbook_delta_message(
            {"type": "orderbook_delta", "seq": seq, "msg": msg}, "session-uuid"
        )
    assert fake.executed_stmts == []


# ---------- orderbook_snapshot expansion -----------------------------------


def test_handle_orderbook_snapshot_expands_one_row_per_level():
    market = _FakeMarket(pk=42, market_id="KXTEST-25")
    fake = _FakeSession(market_lookup_result=market)
    recorder = _RecordingInsert()

    payload = {
        "type": "orderbook_snapshot",
        "seq": 7,
        "msg": {
            "market_ticker": "KXTEST-25",
            "market_id": "uuid-here",
            "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
            "no_dollars_fp": [["0.5400", "20.00"]],
        },
    }

    with patch.object(kalshi_ws, "SessionLocal", return_value=fake), patch.object(
        kalshi_ws, "pg_insert", recorder
    ):
        kalshi_ws.handle_orderbook_snapshot_message(payload, "session-uuid")

    rows = recorder.captured_rows

    # 2 yes levels + 1 no level
    assert len(rows) == 3

    sides = sorted(r["side"] for r in rows)
    assert sides == ["no", "yes", "yes"]

    for row in rows:
        assert row["session_id"] == "session-uuid"
        assert row["seq"] == 7
        assert row["is_snapshot"] is True
        assert row["delta_fp"] is None
        assert row["market_pk"] == 42

    yes_rows = [r for r in rows if r["side"] == "yes"]
    yes_prices = sorted(r["price_dollars"] for r in yes_rows)
    assert yes_prices == [Decimal("0.0800"), Decimal("0.2200")]

    no_row = next(r for r in rows if r["side"] == "no")
    assert no_row["price_dollars"] == Decimal("0.5400")
    assert no_row["size_fp"] == Decimal("20.00")


def test_handle_orderbook_snapshot_with_empty_book_is_a_noop():
    market = _FakeMarket()
    fake = _FakeSession(market_lookup_result=market)

    payload = {
        "type": "orderbook_snapshot",
        "seq": 1,
        "msg": {"market_ticker": "KXTEST-25", "market_id": "uuid-here"},
    }

    with patch.object(kalshi_ws, "SessionLocal", return_value=fake):
        kalshi_ws.handle_orderbook_snapshot_message(payload, "session-uuid")

    assert fake.executed_stmts == []


# ---------- subscription resolver ------------------------------------------


def test_resolve_book_market_tickers_prefers_explicit_config():
    explicit = ["KXFOO-25", "KXBAR-25"]
    with patch.object(kalshi_ws.settings, "kalshi_book_market_tickers", explicit):
        # SessionLocal must NOT be touched when an explicit list is configured.
        with patch.object(
            kalshi_ws, "SessionLocal", side_effect=AssertionError("DB should not be queried")
        ):
            out = kalshi_ws.resolve_book_market_tickers()
    assert out == explicit


def test_resolve_book_market_tickers_falls_back_to_db_when_config_empty():
    expected = [("KX1",), ("KX2",), ("KX3",)]

    class _RowsSession(_FakeSession):
        def query(self, *_a, **_k):
            return self

        def filter(self, *_a, **_k):
            return self

        def order_by(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def all(self):
            return expected

    fake = _RowsSession()
    with patch.object(kalshi_ws.settings, "kalshi_book_market_tickers", []):
        with patch.object(kalshi_ws, "SessionLocal", return_value=fake):
            out = kalshi_ws.resolve_book_market_tickers()

    assert out == ["KX1", "KX2", "KX3"]
    assert fake.closed is True
