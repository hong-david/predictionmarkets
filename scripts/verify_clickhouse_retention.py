"""Verify ClickHouse raw-table TTLs and bytes-by-table.

This is a read-only operator check. Use ``--strict`` in CI/deploy smoke tests
to fail when a raw table is missing or its TTL no longer contains the expected
tier-specific retention clauses.
"""

from __future__ import annotations

import argparse
import json

import httpx

from app.core.config import settings
from app.services.clickhouse_writer import clickhouse_http_auth
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)


EXPECTED_TTL_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "kalshi_trades_raw": (
        "storage_tier = 'observe_only'",
        "INTERVAL 1 HOUR",
        "storage_tier = 'sampled'",
        "INTERVAL 1 DAY",
        "storage_tier = 'hot'",
        "INTERVAL 7 DAY",
        "storage_tier = 'triggered'",
        "INTERVAL 30 DAY",
        "storage_tier = 'case'",
        "INTERVAL 90 DAY",
    ),
    "kalshi_quote_changes_raw": (
        "storage_tier = 'observe_only'",
        "INTERVAL 1 HOUR",
        "storage_tier = 'sampled'",
        "INTERVAL 1 DAY",
        "storage_tier = 'hot'",
        "INTERVAL 3 DAY",
        "storage_tier = 'triggered'",
        "INTERVAL 30 DAY",
        "storage_tier = 'case'",
        "INTERVAL 90 DAY",
    ),
    "kalshi_l2_events_raw": (
        "storage_tier = 'observe_only'",
        "INTERVAL 1 HOUR",
        "storage_tier = 'sampled'",
        "INTERVAL 6 HOUR",
        "storage_tier = 'hot'",
        "INTERVAL 1 DAY",
        "storage_tier = 'triggered'",
        "INTERVAL 7 DAY",
        "storage_tier = 'case'",
        "INTERVAL 30 DAY",
    ),
}


def _query_clickhouse(query: str) -> list[dict]:
    auth = clickhouse_http_auth()
    with httpx.Client(timeout=5.0, auth=auth) as client:
        response = client.post(
            f"{settings.clickhouse_url}/",
            params={
                "database": settings.clickhouse_database,
                "query": f"{query} FORMAT JSON",
            },
        )
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("data")
    return rows if isinstance(rows, list) else []


def verify_clickhouse_retention() -> dict:
    table_names = "', '".join(EXPECTED_TTL_FRAGMENTS)
    table_rows = _query_clickhouse(
        f"""
        select name, create_table_query
        from system.tables
        where database = '{settings.clickhouse_database}'
          and name in ('{table_names}')
        """
    )
    part_rows = _query_clickhouse(
        f"""
        select table,
               sum(rows) as rows,
               sum(bytes_on_disk) as bytes_on_disk,
               min(min_time) as oldest_ts,
               max(max_time) as newest_ts
        from system.parts
        where active
          and database = '{settings.clickhouse_database}'
          and table in ('{table_names}')
        group by table
        """
    )
    create_sql_by_table = {
        str(row.get("name")): str(row.get("create_table_query") or "")
        for row in table_rows
    }
    parts_by_table = {str(row.get("table")): row for row in part_rows}
    tables: list[dict] = []
    ok = True
    for table, fragments in EXPECTED_TTL_FRAGMENTS.items():
        create_sql = create_sql_by_table.get(table, "")
        missing = [fragment for fragment in fragments if fragment not in create_sql]
        if not create_sql or missing:
            ok = False
        parts = parts_by_table.get(table, {})
        tables.append(
            {
                "table": table,
                "exists": bool(create_sql),
                "ttl_ok": bool(create_sql and not missing),
                "missing_ttl_fragments": missing,
                "rows": int(parts.get("rows") or 0),
                "bytes_on_disk": int(parts.get("bytes_on_disk") or 0),
                "oldest_ts": parts.get("oldest_ts"),
                "newest_ts": parts.get("newest_ts"),
            }
        )
    return {
        "ok": ok,
        "database": settings.clickhouse_database,
        "tables": tables,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    run_id = new_run_id("clickhouse-retention")
    mark_pipeline_start(
        "clickhouse_retention",
        detail="Starting ClickHouse TTL verification.",
        run_id=run_id,
    )
    try:
        result = verify_clickhouse_retention()
        mark_pipeline_success(
            "clickhouse_retention",
            detail=(
                "ClickHouse raw TTLs verified."
                if result["ok"]
                else "ClickHouse raw TTL verification found drift."
            ),
            run_id=run_id,
            count=sum(int(t["rows"]) for t in result["tables"]),
            metadata=result,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.strict and not result["ok"]:
            raise SystemExit(1)
    except Exception as exc:
        mark_pipeline_error(
            "clickhouse_retention",
            exc,
            detail="ClickHouse TTL verification failed.",
            run_id=run_id,
        )
        result = {
            "ok": False,
            "database": settings.clickhouse_database,
            "error": str(exc),
            "tables": [],
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.strict:
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
