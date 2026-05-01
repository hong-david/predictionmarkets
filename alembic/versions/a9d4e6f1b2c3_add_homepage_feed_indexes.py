"""Add homepage feed performance indexes.

Revision ID: a9d4e6f1b2c3
Revises: e8b3c2a91d7f
Create Date: 2026-05-01
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "a9d4e6f1b2c3"
down_revision: Union[str, Sequence[str], None] = "e8b3c2a91d7f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_trade_flags_version_score_ts_desc "
            "ON trade_flags (scorer_version, score DESC, ts DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_news_events_pre_news_score_desc "
            "ON news_events (pre_news_trade_score DESC, created_at DESC, market_pk, article_id) "
            "WHERE pre_news_trade_score > 0"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_news_events_status_pre_news_score_desc "
            "ON news_events (status, pre_news_trade_score DESC, created_at DESC, market_pk) "
            "WHERE pre_news_trade_score > 0"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in (
            "ix_news_events_status_pre_news_score_desc",
            "ix_news_events_pre_news_score_desc",
            "ix_trade_flags_version_score_ts_desc",
        ):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
