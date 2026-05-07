"""Add anomaly daily summaries.

Revision ID: 7c2f8a1d9b04
Revises: e2c9a7d1b8f3
Create Date: 2026-05-06
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "7c2f8a1d9b04"
down_revision: Union[str, Sequence[str], None] = "e2c9a7d1b8f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "anomaly_daily_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column("summary_date", sa.Date(), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column(
            "anomaly_count",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("max_score", sa.Numeric(10, 2), nullable=True),
        sa.Column("avg_score", sa.Numeric(10, 2), nullable=True),
        sa.Column("first_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_anomaly_id", sa.Integer(), nullable=True),
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
            "market_pk",
            "summary_date",
            "severity",
            name="uq_anomaly_daily_summaries_market_date_severity",
        ),
    )
    op.create_index(
        "ix_anomaly_daily_summaries_market_pk",
        "anomaly_daily_summaries",
        ["market_pk"],
    )
    op.create_index(
        "ix_anomaly_daily_summaries_summary_date",
        "anomaly_daily_summaries",
        ["summary_date"],
    )
    op.create_index(
        "ix_anomaly_daily_summaries_severity",
        "anomaly_daily_summaries",
        ["severity"],
    )
    op.create_index(
        "ix_anomaly_daily_summaries_market_date",
        "anomaly_daily_summaries",
        ["market_pk", "summary_date"],
    )
    op.create_index(
        "ix_anomaly_daily_summaries_severity_date",
        "anomaly_daily_summaries",
        ["severity", "summary_date"],
    )


def downgrade() -> None:
    op.drop_table("anomaly_daily_summaries")
