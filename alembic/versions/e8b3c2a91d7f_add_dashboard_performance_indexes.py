"""Add dashboard performance indexes.

Revision ID: e8b3c2a91d7f
Revises: b7d9e4c2a801
Create Date: 2026-05-01
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "e8b3c2a91d7f"
down_revision: Union[str, Sequence[str], None] = "b7d9e4c2a801"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_market_snapshots_market_ts_id_desc "
            "ON market_snapshots (market_pk, ts DESC, id DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_trades_market_ts_id_desc "
            "ON trades (market_pk, ts DESC, id DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_trades_ts_desc "
            "ON trades (ts DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_anomalies_market_created_id_desc "
            "ON anomalies (market_pk, created_at DESC, id DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_anomalies_created_id_desc "
            "ON anomalies (created_at DESC, id DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_anomalies_severity_created_desc "
            "ON anomalies (severity, created_at DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_book_events_market_received_id_desc "
            "ON book_events (market_pk, received_at DESC, id DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_book_events_received_desc "
            "ON book_events (received_at DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_market_metrics_trade_count_desc "
            "ON market_metrics (trade_count DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_market_metrics_last_trade_ts_desc "
            "ON market_metrics (last_trade_ts DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_market_metrics_updated_at_desc "
            "ON market_metrics (updated_at DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_markets_status_close_time "
            "ON markets (status, close_time)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_markets_category_prior "
            "ON markets (category, manipulability_prior)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_news_events_market_relevance_created "
            "ON news_events (market_pk, relevance_score, created_at DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in (
            "ix_news_events_market_relevance_created",
            "ix_markets_category_prior",
            "ix_markets_status_close_time",
            "ix_market_metrics_updated_at_desc",
            "ix_market_metrics_last_trade_ts_desc",
            "ix_market_metrics_trade_count_desc",
            "ix_book_events_received_desc",
            "ix_book_events_market_received_id_desc",
            "ix_anomalies_severity_created_desc",
            "ix_anomalies_created_id_desc",
            "ix_anomalies_market_created_id_desc",
            "ix_trades_ts_desc",
            "ix_trades_market_ts_id_desc",
            "ix_market_snapshots_market_ts_id_desc",
        ):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
