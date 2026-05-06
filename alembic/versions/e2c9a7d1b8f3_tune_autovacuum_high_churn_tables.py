"""Tune autovacuum thresholds for high-churn tables.

Revision ID: e2c9a7d1b8f3
Revises: d4f0a2b7c9e1
Create Date: 2026-05-06 08:30:00.000000
"""

from alembic import op


revision = "e2c9a7d1b8f3"
down_revision = "d4f0a2b7c9e1"
branch_labels = None
depends_on = None


TABLE_OPTIONS = {
    "market_snapshots": (
        "autovacuum_vacuum_scale_factor = 0.05",
        "autovacuum_analyze_scale_factor = 0.02",
    ),
    "book_events": (
        "autovacuum_vacuum_scale_factor = 0.05",
        "autovacuum_analyze_scale_factor = 0.05",
    ),
    "market_metrics": (
        "autovacuum_vacuum_scale_factor = 0.02",
        "autovacuum_analyze_scale_factor = 0.02",
    ),
    "markets": (
        "autovacuum_vacuum_scale_factor = 0.05",
        "autovacuum_analyze_scale_factor = 0.05",
    ),
    "market_news_profiles": (
        "autovacuum_vacuum_scale_factor = 0.05",
        "autovacuum_analyze_scale_factor = 0.05",
    ),
    "pipeline_heartbeats": (
        "autovacuum_vacuum_scale_factor = 0",
        "autovacuum_vacuum_threshold = 50",
        "autovacuum_analyze_scale_factor = 0",
        "autovacuum_analyze_threshold = 50",
    ),
}


def upgrade() -> None:
    for table_name, options in TABLE_OPTIONS.items():
        op.execute(f"ALTER TABLE {table_name} SET ({', '.join(options)})")


def downgrade() -> None:
    for table_name, options in TABLE_OPTIONS.items():
        reset_options = ", ".join(option.split("=", 1)[0].strip() for option in options)
        op.execute(f"ALTER TABLE {table_name} RESET ({reset_options})")
