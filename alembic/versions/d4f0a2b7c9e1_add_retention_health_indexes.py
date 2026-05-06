"""Add retention and health dashboard indexes.

Revision ID: d4f0a2b7c9e1
Revises: a9d4e6f1b2c3
Create Date: 2026-05-06
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "d4f0a2b7c9e1"
down_revision: Union[str, Sequence[str], None] = "a9d4e6f1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_market_snapshots_market_ts_id "
            "ON market_snapshots (market_pk, ts, id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_market_metrics_tier_market_snapshot "
            "ON market_metrics (storage_tier, market_pk, latest_snapshot_id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_news_events_created_id_desc "
            "ON news_events (created_at DESC, id DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_trade_flags_created_id_desc "
            "ON trade_flags (created_at DESC, id DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in (
            "ix_trade_flags_created_id_desc",
            "ix_news_events_created_id_desc",
            "ix_market_metrics_tier_market_snapshot",
            "ix_market_snapshots_market_ts_id",
        ):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
