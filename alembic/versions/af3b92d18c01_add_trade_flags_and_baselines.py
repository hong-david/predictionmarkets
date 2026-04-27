"""Add trade flags and peer baselines.

Revision ID: af3b92d18c01
Revises: 9c0d4e5f6a71
Create Date: 2026-04-27
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "af3b92d18c01"
down_revision: Union[str, Sequence[str], None] = "9c0d4e5f6a71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_baselines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column(
            "subcategory", sa.String(length=64), nullable=False, server_default="*"
        ),
        sa.Column(
            "liquidity_bucket",
            sa.String(length=32),
            nullable=False,
            server_default="all",
        ),
        sa.Column(
            "time_to_close_bucket",
            sa.String(length=32),
            nullable=False,
            server_default="all",
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("count_p95", sa.Float(), nullable=False, server_default="0"),
        sa.Column("count_p99", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "abs_price_delta_p95", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("impact_p95", sa.Float(), nullable=False, server_default="0"),
        sa.Column("impact_p99", sa.Float(), nullable=False, server_default="0"),
        sa.Column("scorer_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "category",
            "subcategory",
            "liquidity_bucket",
            "time_to_close_bucket",
            "window_start",
            "window_end",
            "scorer_version",
            name="uq_trade_baselines_scope_window_version",
        ),
    )
    op.create_index("ix_trade_baselines_category", "trade_baselines", ["category"])
    op.create_index(
        "ix_trade_baselines_subcategory", "trade_baselines", ["subcategory"]
    )
    op.create_index(
        "ix_trade_baselines_liquidity_bucket", "trade_baselines", ["liquidity_bucket"]
    )
    op.create_index(
        "ix_trade_baselines_time_to_close_bucket",
        "trade_baselines",
        ["time_to_close_bucket"],
    )
    op.create_index(
        "ix_trade_baselines_window_start", "trade_baselines", ["window_start"]
    )
    op.create_index("ix_trade_baselines_window_end", "trade_baselines", ["window_end"])
    op.create_index(
        "ix_trade_baselines_scorer_version", "trade_baselines", ["scorer_version"]
    )
    op.create_index(
        "ix_trade_baselines_scope_latest",
        "trade_baselines",
        ["category", "subcategory", "window_end"],
    )

    op.create_table(
        "trade_flags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_pk", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.String(length=128), nullable=False),
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("local_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("context_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "severity", sa.String(length=16), nullable=False, server_default="low"
        ),
        sa.Column(
            "reasons", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")
        ),
        sa.Column(
            "components",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "features", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")
        ),
        sa.Column("scorer_version", sa.Integer(), nullable=False),
        sa.Column("promoted_storage_tier", sa.String(length=24), nullable=True),
        sa.Column("case_evidence_id", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["case_evidence_id"], ["case_evidence.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["market_pk"], ["markets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trade_pk"], ["trades.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trade_pk", "scorer_version", name="uq_trade_flags_trade_version"
        ),
    )
    op.create_index("ix_trade_flags_trade_pk", "trade_flags", ["trade_pk"])
    op.create_index("ix_trade_flags_trade_id", "trade_flags", ["trade_id"])
    op.create_index("ix_trade_flags_market_pk", "trade_flags", ["market_pk"])
    op.create_index("ix_trade_flags_ts", "trade_flags", ["ts"])
    op.create_index("ix_trade_flags_score_ts", "trade_flags", ["score", "ts"])
    op.create_index(
        "ix_trade_flags_market_score", "trade_flags", ["market_pk", "score"]
    )
    op.create_index("ix_trade_flags_severity", "trade_flags", ["severity"])
    op.create_index("ix_trade_flags_scorer_version", "trade_flags", ["scorer_version"])
    op.create_index(
        "ix_trade_flags_promoted_storage_tier", "trade_flags", ["promoted_storage_tier"]
    )
    op.create_index(
        "ix_trade_flags_case_evidence_id", "trade_flags", ["case_evidence_id"]
    )


def downgrade() -> None:
    op.drop_table("trade_flags")
    op.drop_table("trade_baselines")
