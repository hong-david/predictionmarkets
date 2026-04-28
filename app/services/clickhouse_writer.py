"""Tiny HTTP writer for ClickHouse JSONEachRow inserts.

This keeps ClickHouse optional at runtime: if the service is down, callers can
log and continue writing the Postgres control-plane/projection rows.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
import time
from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def clickhouse_http_auth() -> tuple[str, str] | None:
    if settings.clickhouse_password:
        return (settings.clickhouse_user or "default", settings.clickhouse_password)
    if settings.clickhouse_user and settings.clickhouse_user != "default":
        return (settings.clickhouse_user, "")
    return None


def _json_default(value):
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"{type(value)!r} is not JSON serializable")


class ClickHouseWriter:
    def __init__(self, *, timeout: float = 5.0) -> None:
        self.timeout = timeout

    async def insert_json_each_row(self, table: str, rows: Iterable[dict]) -> int:
        payload_rows = list(rows)
        if not payload_rows:
            return 0
        body = "\n".join(
            json.dumps(row, default=_json_default, separators=(",", ":"))
            for row in payload_rows
        )
        query = f"INSERT INTO {table} FORMAT JSONEachRow"
        auth = clickhouse_http_auth()
        async with httpx.AsyncClient(timeout=self.timeout, auth=auth) as client:
            response = await client.post(
                f"{settings.clickhouse_url}/",
                params={"database": settings.clickhouse_database, "query": query},
                content=body,
            )
            response.raise_for_status()
        return len(payload_rows)

    def insert_json_each_row_sync(self, table: str, rows: Iterable[dict]) -> int:
        payload_rows = list(rows)
        if not payload_rows:
            return 0
        body = "\n".join(
            json.dumps(row, default=_json_default, separators=(",", ":"))
            for row in payload_rows
        )
        query = f"INSERT INTO {table} FORMAT JSONEachRow"
        auth = clickhouse_http_auth()
        with httpx.Client(timeout=self.timeout, auth=auth) as client:
            response = client.post(
                f"{settings.clickhouse_url}/",
                params={"database": settings.clickhouse_database, "query": query},
                content=body,
            )
            response.raise_for_status()
        return len(payload_rows)


class ClickHouseBatcher:
    """Thread-backed JSONEachRow batcher for sync ingest handlers."""

    def __init__(
        self,
        *,
        max_rows: int,
        flush_interval_sec: float,
        queue_size: int,
        writer: ClickHouseWriter | None = None,
    ) -> None:
        self.max_rows = max(1, max_rows)
        self.flush_interval_sec = max(0.05, flush_interval_sec)
        self._writer = writer or ClickHouseWriter()
        self._queue: queue.Queue[tuple[str, dict] | None] = queue.Queue(
            maxsize=max(1, queue_size)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="clickhouse-batcher",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=timeout)

    def enqueue(self, table: str, row: dict) -> bool:
        self.start()
        try:
            self._queue.put_nowait((table, row))
            return True
        except queue.Full:
            logger.warning(
                "ClickHouse batch queue full; dropping row for table=%s", table
            )
            return False

    def _run(self) -> None:
        buffers: dict[str, list[dict]] = {}
        last_flush = time.monotonic()
        while not self._stop.is_set():
            timeout = max(
                0.01, self.flush_interval_sec - (time.monotonic() - last_flush)
            )
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None

            if item is None:
                now = time.monotonic()
                if buffers and (
                    self._stop.is_set() or now - last_flush >= self.flush_interval_sec
                ):
                    self._flush(buffers)
                    last_flush = now
                continue

            table, row = item
            buf = buffers.setdefault(table, [])
            buf.append(row)
            if len(buf) >= self.max_rows:
                self._flush_table(table, buf)
                buffers[table] = []
                last_flush = time.monotonic()

        if buffers:
            self._flush(buffers)

    def _flush(self, buffers: dict[str, list[dict]]) -> None:
        for table, rows in list(buffers.items()):
            if rows:
                self._flush_table(table, rows)
                buffers[table] = []

    def _flush_table(self, table: str, rows: list[dict]) -> None:
        try:
            inserted = self._writer.insert_json_each_row_sync(table, rows)
            logger.debug("Inserted %s rows into ClickHouse table=%s", inserted, table)
        except Exception as exc:
            logger.warning(
                "ClickHouse batch insert failed table=%s rows=%s: %s",
                table,
                len(rows),
                exc,
            )


clickhouse_batcher = ClickHouseBatcher(
    max_rows=settings.clickhouse_batch_max_rows,
    flush_interval_sec=settings.clickhouse_batch_flush_interval_sec,
    queue_size=settings.clickhouse_batch_queue_size,
)
atexit.register(clickhouse_batcher.stop)
