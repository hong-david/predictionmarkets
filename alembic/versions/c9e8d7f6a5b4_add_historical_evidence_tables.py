"""Add durable historical evidence tables.

Revision ID: c9e8d7f6a5b4
Revises: b8f2d1c3a4e5
Create Date: 2026-05-11
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9e8d7f6a5b4"
down_revision: Union[str, Sequence[str], None] = "b8f2d1c3a4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column("source_trade_pk", sa.Integer(), nullable=True),
        sa.Column("source_trade_flag_pk", sa.Integer(), nullable=True),
        sa.Column("trade_id", sa.String(length=128), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("yes_price_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("no_price_dollars", sa.Numeric(12, 4), nullable=True),
        sa.Column("count_fp", sa.Numeric(18, 2), nullable=True),
        sa.Column("taker_side", sa.String(length=3), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("local_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("context_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="low"),
        sa.Column(
            "reasons",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "components",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "features",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("scorer_version", sa.Integer(), nullable=False),
        sa.Column("storage_tier", sa.String(length=24), nullable=True),
        sa.Column("retention_reason", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trade_id",
            "scorer_version",
            name="uq_trade_evidence_trade_version",
        ),
    )
    op.create_index("ix_trade_evidence_market_pk", "trade_evidence", ["market_pk"])
    op.create_index(
        "ix_trade_evidence_source_trade_pk",
        "trade_evidence",
        ["source_trade_pk"],
    )
    op.create_index(
        "ix_trade_evidence_source_trade_flag_pk",
        "trade_evidence",
        ["source_trade_flag_pk"],
    )
    op.create_index("ix_trade_evidence_trade_id", "trade_evidence", ["trade_id"])
    op.create_index("ix_trade_evidence_ts", "trade_evidence", ["ts"])
    op.create_index("ix_trade_evidence_severity", "trade_evidence", ["severity"])
    op.create_index(
        "ix_trade_evidence_scorer_version",
        "trade_evidence",
        ["scorer_version"],
    )
    op.create_index(
        "ix_trade_evidence_storage_tier",
        "trade_evidence",
        ["storage_tier"],
    )
    op.create_index(
        "ix_trade_evidence_market_score",
        "trade_evidence",
        ["market_pk", "score"],
    )
    op.create_index(
        "ix_trade_evidence_score_ts",
        "trade_evidence",
        ["score", "ts"],
    )
    op.create_index(
        "ix_trade_evidence_market_ts",
        "trade_evidence",
        ["market_pk", "ts"],
    )

    op.create_table(
        "anomaly_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column("source_anomaly_pk", sa.Integer(), nullable=True),
        sa.Column("source_snapshot_pk", sa.Integer(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.Numeric(10, 2), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column(
            "reasons",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "signals",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("storage_tier", sa.String(length=24), nullable=True),
        sa.Column("retention_reason", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_anomaly_pk",
            name="uq_anomaly_evidence_source_anomaly",
        ),
    )
    op.create_index("ix_anomaly_evidence_market_pk", "anomaly_evidence", ["market_pk"])
    op.create_index(
        "ix_anomaly_evidence_source_anomaly_pk",
        "anomaly_evidence",
        ["source_anomaly_pk"],
    )
    op.create_index(
        "ix_anomaly_evidence_source_snapshot_pk",
        "anomaly_evidence",
        ["source_snapshot_pk"],
    )
    op.create_index("ix_anomaly_evidence_ts", "anomaly_evidence", ["ts"])
    op.create_index("ix_anomaly_evidence_severity", "anomaly_evidence", ["severity"])
    op.create_index(
        "ix_anomaly_evidence_storage_tier",
        "anomaly_evidence",
        ["storage_tier"],
    )
    op.create_index(
        "ix_anomaly_evidence_market_score",
        "anomaly_evidence",
        ["market_pk", "score"],
    )
    op.create_index(
        "ix_anomaly_evidence_score_ts",
        "anomaly_evidence",
        ["score", "ts"],
    )
    op.create_index(
        "ix_anomaly_evidence_market_ts",
        "anomaly_evidence",
        ["market_pk", "ts"],
    )
    op.execute(
        "ALTER TABLE trade_evidence SET ("
        "autovacuum_vacuum_scale_factor = 0.05, "
        "autovacuum_analyze_scale_factor = 0.02)"
    )
    op.execute(
        "ALTER TABLE anomaly_evidence SET ("
        "autovacuum_vacuum_scale_factor = 0.05, "
        "autovacuum_analyze_scale_factor = 0.02)"
    )


def downgrade() -> None:
    op.drop_table("anomaly_evidence")
    op.drop_table("trade_evidence")
