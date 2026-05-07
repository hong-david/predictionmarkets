"""Add compact market price history.

Revision ID: b8f2d1c3a4e5
Revises: 7c2f8a1d9b04
Create Date: 2026-05-07
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8f2d1c3a4e5"
down_revision: Union[str, Sequence[str], None] = "7c2f8a1d9b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_price_history",
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column("interval_sec", sa.Integer(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_price_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("high_price_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("low_price_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("close_price_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("close_price_source", sa.String(length=16), nullable=True),
        sa.Column(
            "close_price_source_rank",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("first_price_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_price_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_yes_bid_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("high_yes_bid_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("low_yes_bid_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("close_yes_bid_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("open_yes_ask_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("high_yes_ask_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("low_yes_ask_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("close_yes_ask_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("close_volume_24h_fp", sa.Numeric(18, 2), nullable=True),
        sa.Column("close_open_interest_fp", sa.Numeric(18, 2), nullable=True),
        sa.Column("first_quote_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_quote_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trade_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "trade_volume_contracts",
            sa.Numeric(18, 2),
            server_default="0",
            nullable=False,
        ),
        sa.Column("quote_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["market_pk"], ["markets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("market_pk", "interval_sec", "bucket_start"),
    )
    op.create_index(
        "ix_market_price_history_bucket",
        "market_price_history",
        ["bucket_start"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_price_history_bucket", table_name="market_price_history")
    op.drop_table("market_price_history")
