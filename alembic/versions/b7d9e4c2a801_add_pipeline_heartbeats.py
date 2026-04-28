"""Add pipeline heartbeats.

Revision ID: b7d9e4c2a801
Revises: af3b92d18c01
Create Date: 2026-04-27
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7d9e4c2a801"
down_revision: Union[str, Sequence[str], None] = "af3b92d18c01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipeline_heartbeats",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column(
            "component_type",
            sa.String(length=32),
            nullable=False,
            server_default="job",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="starting",
        ),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("count", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "heartbeat_count",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "success_count",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "error_count",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(
        "ix_pipeline_heartbeats_component_type",
        "pipeline_heartbeats",
        ["component_type"],
    )
    op.create_index("ix_pipeline_heartbeats_status", "pipeline_heartbeats", ["status"])
    op.create_index(
        "ix_pipeline_heartbeats_run_id", "pipeline_heartbeats", ["run_id"]
    )
    op.create_index(
        "ix_pipeline_heartbeats_last_heartbeat_at",
        "pipeline_heartbeats",
        ["last_heartbeat_at"],
    )
    op.create_index(
        "ix_pipeline_heartbeats_last_success_at",
        "pipeline_heartbeats",
        ["last_success_at"],
    )
    op.create_index(
        "ix_pipeline_heartbeats_last_error_at",
        "pipeline_heartbeats",
        ["last_error_at"],
    )
    op.create_index(
        "ix_pipeline_heartbeats_updated_at",
        "pipeline_heartbeats",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_table("pipeline_heartbeats")
