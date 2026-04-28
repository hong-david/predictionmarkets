"""Storage pressure and retention visibility helpers.

The dashboard only needs a compact JSON snapshot, but keeping the checks in a
service module lets scripts reuse the same logic for operator reports.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.clickhouse_writer import clickhouse_http_auth

_HTTP_TIMEOUT_SEC = 1.5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bytes(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ratio(used: int, total: int) -> float | None:
    if total <= 0:
        return None
    return max(0.0, min(1.0, used / total))


def _pressure_status(used_ratio: float | None) -> str:
    if used_ratio is None:
        return "empty"
    if used_ratio >= settings.storage_error_used_ratio:
        return "error"
    if used_ratio >= settings.storage_warning_used_ratio:
        return "stale"
    return "healthy"


def _worst_status(statuses: list[str]) -> str:
    rank = {"healthy": 0, "empty": 1, "stale": 2, "error": 3}
    if not statuses:
        return "empty"
    return max(statuses, key=lambda status: rank.get(status, 0))


def _postgres_storage(db: Session, *, table_limit: int) -> dict:
    database_row = db.execute(
        text(
            """
            select current_database() as database_name,
                   pg_database_size(current_database()) as database_bytes
            """
        )
    ).mappings().one()
    table_rows = (
        db.execute(
            text(
                """
                select schemaname,
                       relname as table_name,
                       pg_total_relation_size(relid) as total_bytes,
                       pg_relation_size(relid) as table_bytes,
                       pg_indexes_size(relid) as index_bytes,
                       n_live_tup::bigint as estimated_rows,
                       n_dead_tup::bigint as estimated_dead_rows,
                       last_vacuum,
                       last_autovacuum,
                       last_analyze,
                       last_autoanalyze
                from pg_stat_user_tables
                order by pg_total_relation_size(relid) desc
                limit :limit
                """
            ),
            {"limit": table_limit},
        )
        .mappings()
        .all()
    )
    return {
        "status": "healthy",
        "database_name": database_row["database_name"],
        "database_bytes": _bytes(database_row["database_bytes"]),
        "largest_tables": [
            {
                "schema": row["schemaname"],
                "table": row["table_name"],
                "total_bytes": _bytes(row["total_bytes"]),
                "table_bytes": _bytes(row["table_bytes"]),
                "index_bytes": _bytes(row["index_bytes"]),
                "estimated_rows": _bytes(row["estimated_rows"]),
                "estimated_dead_rows": _bytes(row["estimated_dead_rows"]),
                "last_vacuum": row["last_vacuum"].isoformat()
                if row["last_vacuum"]
                else None,
                "last_autovacuum": row["last_autovacuum"].isoformat()
                if row["last_autovacuum"]
                else None,
                "last_analyze": row["last_analyze"].isoformat()
                if row["last_analyze"]
                else None,
                "last_autoanalyze": row["last_autoanalyze"].isoformat()
                if row["last_autoanalyze"]
                else None,
            }
            for row in table_rows
        ],
    }


def _clickhouse_query(query: str) -> list[dict]:
    auth = clickhouse_http_auth()
    with httpx.Client(timeout=_HTTP_TIMEOUT_SEC, auth=auth) as client:
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


def _clickhouse_storage() -> dict:
    try:
        disks = _clickhouse_query(
            """
            select name,
                   path,
                   free_space,
                   total_space,
                   keep_free_space
            from system.disks
            """
        )
        parts = _clickhouse_query(
            f"""
            select table,
                   sum(rows) as rows,
                   sum(bytes_on_disk) as bytes_on_disk
            from system.parts
            where active and database = '{settings.clickhouse_database}'
            group by table
            order by bytes_on_disk desc
            """
        )
        disk_payload = []
        statuses = []
        for disk in disks:
            total = _bytes(disk.get("total_space"))
            free = _bytes(disk.get("free_space"))
            used = max(0, total - free)
            used_ratio = _ratio(used, total)
            status = _pressure_status(used_ratio)
            statuses.append(status)
            disk_payload.append(
                {
                    "name": disk.get("name"),
                    "path": disk.get("path"),
                    "total_bytes": total,
                    "free_bytes": free,
                    "used_bytes": used,
                    "used_ratio": used_ratio,
                    "keep_free_space_bytes": _bytes(disk.get("keep_free_space")),
                    "status": status,
                }
            )
        return {
            "status": _worst_status(statuses) if disk_payload else "empty",
            "disks": disk_payload,
            "tables": [
                {
                    "table": part.get("table"),
                    "rows": _bytes(part.get("rows")),
                    "bytes_on_disk": _bytes(part.get("bytes_on_disk")),
                }
                for part in parts
            ],
        }
    except Exception as exc:
        return {
            "status": "error"
            if settings.kalshi_raw_backend in {"clickhouse", "dual"}
            else "empty",
            "error": str(exc),
            "optional": settings.kalshi_raw_backend == "postgres",
        }


def _opensearch_storage() -> dict:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SEC) as client:
            health = client.get(f"{settings.search_url}/_cluster/health")
            health.raise_for_status()
            indices = client.get(
                f"{settings.search_url}/_cat/indices",
                params={"format": "json", "bytes": "b"},
            )
            indices.raise_for_status()
            fs = client.get(f"{settings.search_url}/_nodes/stats/fs")
            fs.raise_for_status()
        health_payload = health.json()
        index_payload = indices.json()
        fs_payload = fs.json()
        disk_statuses = []
        nodes = []
        for node_id, node in (fs_payload.get("nodes") or {}).items():
            total = _bytes((node.get("fs") or {}).get("total", {}).get("total_in_bytes"))
            free = _bytes((node.get("fs") or {}).get("total", {}).get("free_in_bytes"))
            used = max(0, total - free)
            used_ratio = _ratio(used, total)
            status = _pressure_status(used_ratio)
            disk_statuses.append(status)
            nodes.append(
                {
                    "node_id": node_id,
                    "name": node.get("name"),
                    "total_bytes": total,
                    "free_bytes": free,
                    "used_bytes": used,
                    "used_ratio": used_ratio,
                    "status": status,
                }
            )
        cluster_status = str(health_payload.get("status") or "unknown")
        statuses = disk_statuses or ["healthy"]
        if cluster_status == "red":
            statuses.append("error")
        elif cluster_status == "yellow":
            statuses.append("stale")
        return {
            "status": _worst_status(statuses),
            "cluster_status": cluster_status,
            "nodes": nodes,
            "indices": [
                {
                    "index": row.get("index"),
                    "docs_count": _bytes(row.get("docs.count")),
                    "store_bytes": _bytes(row.get("store.size")),
                    "health": row.get("health"),
                }
                for row in index_payload
                if str(row.get("index") or "").startswith(
                    settings.search_index_prefix
                )
            ],
        }
    except Exception as exc:
        return {"status": "empty", "error": str(exc), "optional": True}


def _local_disk_storage() -> dict:
    usage = shutil.disk_usage(".")
    used_ratio = _ratio(usage.used, usage.total)
    return {
        "status": _pressure_status(used_ratio),
        "path": ".",
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_ratio": used_ratio,
    }


def storage_health_snapshot(db: Session, *, table_limit: int = 8) -> dict:
    """Return a defensive storage snapshot for dashboard/operator use."""

    generated_at = _utc_now()
    postgres = _postgres_storage(db, table_limit=table_limit)
    clickhouse = _clickhouse_storage()
    opensearch = _opensearch_storage()
    local_disk = _local_disk_storage()
    components = {
        "postgres": postgres,
        "clickhouse": clickhouse,
        "opensearch": opensearch,
        "local_disk": local_disk,
    }
    statuses = [
        component.get("status", "empty")
        for component in components.values()
        if not (component.get("optional") and component.get("status") == "empty")
    ]
    return {
        "generated_at": generated_at.isoformat(),
        "summary": {
            "status": _worst_status(statuses),
            "warning_used_ratio": settings.storage_warning_used_ratio,
            "error_used_ratio": settings.storage_error_used_ratio,
        },
        **components,
    }


def storage_health_detail(snapshot: dict) -> str:
    """Compact human-readable storage line for the pipeline widget."""

    pg_bytes = _bytes((snapshot.get("postgres") or {}).get("database_bytes"))
    largest = ((snapshot.get("postgres") or {}).get("largest_tables") or [{}])[0]
    largest_name = largest.get("table")
    largest_bytes = _bytes(largest.get("total_bytes"))
    local = snapshot.get("local_disk") or {}
    used_ratio = local.get("used_ratio")
    used_text = f"{used_ratio * 100:.0f}%" if isinstance(used_ratio, float) else "n/a"
    parts = [
        f"Postgres {pg_bytes / (1024**3):.2f} GiB",
        f"local disk {used_text} used",
    ]
    if largest_name:
        parts.append(f"largest table {largest_name} {largest_bytes / (1024**3):.2f} GiB")
    ch_status = (snapshot.get("clickhouse") or {}).get("status")
    if ch_status:
        parts.append(f"ClickHouse {ch_status}")
    search_status = (snapshot.get("opensearch") or {}).get("status")
    if search_status:
        parts.append(f"OpenSearch {search_status}")
    return "; ".join(parts) + "."
