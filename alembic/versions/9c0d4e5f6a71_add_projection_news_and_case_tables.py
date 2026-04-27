"""Add projection, feature, news-linking, and evidence tables.

Revision ID: 9c0d4e5f6a71
Revises: f1a2c3d4b5e6
Create Date: 2026-04-26
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "9c0d4e5f6a71"
down_revision: Union[str, Sequence[str], None] = "f1a2c3d4b5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_snapshots_market_ts_id_desc",
        "market_snapshots",
        ["market_pk", sa.text("ts DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_snapshots_market_id_desc",
        "market_snapshots",
        ["market_pk", sa.text("id DESC")],
    )
    op.create_index(
        "ix_trades_ts_id_desc", "trades", [sa.text("ts DESC"), sa.text("id DESC")]
    )
    op.create_index(
        "ix_trades_market_ts_id_desc",
        "trades",
        ["market_pk", sa.text("ts DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_book_events_market_received_desc",
        "book_events",
        ["market_pk", sa.text("received_at DESC")],
    )
    op.create_index(
        "ix_book_events_delta_recent",
        "book_events",
        ["market_pk", sa.text("received_at DESC")],
        postgresql_where=sa.text("is_snapshot = false"),
    )
    op.create_index(
        "ix_anomalies_market_created_desc",
        "anomalies",
        ["market_pk", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_anomalies_severity_created_desc",
        "anomalies",
        ["severity", sa.text("created_at DESC"), sa.text("id DESC")],
    )

    op.create_table(
        "market_metrics",
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column("latest_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("latest_snapshot_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_price_cents", sa.Integer(), nullable=True),
        sa.Column("yes_bid_cents", sa.Integer(), nullable=True),
        sa.Column("yes_ask_cents", sa.Integer(), nullable=True),
        sa.Column("no_bid_cents", sa.Integer(), nullable=True),
        sa.Column("no_ask_cents", sa.Integer(), nullable=True),
        sa.Column("volume_24h_contracts", sa.BigInteger(), nullable=True),
        sa.Column("open_interest_contracts", sa.BigInteger(), nullable=True),
        sa.Column("liquidity_cents", sa.BigInteger(), nullable=True),
        sa.Column("trade_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("anomaly_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "high_anomaly_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("last_trade_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_anomaly_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("urgency_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "storage_tier",
            sa.String(length=24),
            nullable=False,
            server_default="observe_only",
        ),
        sa.Column("retention_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "retention_reasons",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["market_pk"], ["markets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("market_pk"),
    )
    op.create_index(
        "ix_market_metrics_latest_snapshot_id", "market_metrics", ["latest_snapshot_id"]
    )
    op.create_index(
        "ix_market_metrics_latest_snapshot_ts", "market_metrics", ["latest_snapshot_ts"]
    )
    op.create_index(
        "ix_market_metrics_tier_updated",
        "market_metrics",
        ["storage_tier", "updated_at"],
    )
    op.create_index(
        "ix_market_metrics_urgency_market",
        "market_metrics",
        ["urgency_score", "market_pk"],
    )

    op.create_table(
        "market_features_1m",
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "trade_volume_contracts",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_trade_size_contracts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("first_price_cents", sa.Integer(), nullable=True),
        sa.Column("last_price_cents", sa.Integer(), nullable=True),
        sa.Column("min_price_cents", sa.Integer(), nullable=True),
        sa.Column("max_price_cents", sa.Integer(), nullable=True),
        sa.Column("quote_updates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_spread_cents", sa.Integer(), nullable=True),
        sa.Column(
            "cancel_volume_contracts",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "add_volume_contracts", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("depth_pull_score", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["market_pk"], ["markets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("market_pk", "bucket_start"),
    )
    op.create_index(
        "ix_market_features_1m_bucket", "market_features_1m", ["bucket_start"]
    )

    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("canonical_url_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("source_tier", sa.String(length=32), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("entities", sa.JSON(), nullable=True),
        sa.Column("keywords", sa.JSON(), nullable=True),
        sa.Column("embedding_vector_id", sa.String(length=128), nullable=True),
        sa.Column("raw_body_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_news_articles_canonical_url_hash",
        "news_articles",
        ["canonical_url_hash"],
        unique=True,
    )
    op.create_index("ix_news_articles_domain", "news_articles", ["domain"])
    op.create_index(
        "ix_news_articles_first_seen_at", "news_articles", ["first_seen_at"]
    )
    op.create_index("ix_news_articles_published_at", "news_articles", ["published_at"])
    op.create_index(
        "ix_news_articles_raw_body_expires_at", "news_articles", ["raw_body_expires_at"]
    )
    op.create_index("ix_news_articles_source_tier", "news_articles", ["source_tier"])

    op.create_table(
        "market_news_profiles",
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column(
            "normalized_keywords",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "entities", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")
        ),
        sa.Column(
            "aliases", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")
        ),
        sa.Column("jurisdiction", sa.String(length=64), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("active_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding_vector_id", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["market_pk"], ["markets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("market_pk"),
    )
    op.create_index(
        "ix_market_news_profiles_active_from", "market_news_profiles", ["active_from"]
    )
    op.create_index(
        "ix_market_news_profiles_active_to", "market_news_profiles", ["active_to"]
    )
    op.create_index(
        "ix_market_news_profiles_category", "market_news_profiles", ["category"]
    )
    op.create_index(
        "ix_market_news_profiles_jurisdiction", "market_news_profiles", ["jurisdiction"]
    )

    op.create_table(
        "news_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "pre_news_trade_score", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("leakage_window_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="candidate"
        ),
        sa.Column(
            "score_components",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"], ["news_articles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["market_pk"], ["markets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "article_id", "market_pk", name="uq_news_events_article_market"
        ),
    )
    op.create_index("ix_news_events_article_id", "news_events", ["article_id"])
    op.create_index("ix_news_events_created_at", "news_events", ["created_at"])
    op.create_index("ix_news_events_market_pk", "news_events", ["market_pk"])
    op.create_index(
        "ix_news_events_market_score",
        "news_events",
        ["market_pk", "pre_news_trade_score"],
    )
    op.create_index("ix_news_events_status", "news_events", ["status"])

    op.create_table(
        "case_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_key", sa.String(length=128), nullable=False),
        sa.Column("market_pk", sa.Integer(), nullable=False),
        sa.Column("trigger_kind", sa.String(length=32), nullable=False),
        sa.Column("trigger_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column(
            "payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")
        ),
        sa.Column("classifier_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["market_pk"], ["markets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_case_evidence_case_key", "case_evidence", ["case_key"], unique=True
    )
    op.create_index("ix_case_evidence_created_at", "case_evidence", ["created_at"])
    op.create_index("ix_case_evidence_market_pk", "case_evidence", ["market_pk"])
    op.create_index("ix_case_evidence_trigger_kind", "case_evidence", ["trigger_kind"])
    op.create_index("ix_case_evidence_trigger_ts", "case_evidence", ["trigger_ts"])
    op.create_index("ix_case_evidence_window_end", "case_evidence", ["window_end"])
    op.create_index("ix_case_evidence_window_start", "case_evidence", ["window_start"])


def downgrade() -> None:
    op.drop_table("case_evidence")
    op.drop_table("news_events")
    op.drop_table("market_news_profiles")
    op.drop_table("news_articles")
    op.drop_table("market_features_1m")
    op.drop_table("market_metrics")
    op.drop_index("ix_anomalies_severity_created_desc", table_name="anomalies")
    op.drop_index("ix_anomalies_market_created_desc", table_name="anomalies")
    op.drop_index("ix_book_events_delta_recent", table_name="book_events")
    op.drop_index("ix_book_events_market_received_desc", table_name="book_events")
    op.drop_index("ix_trades_market_ts_id_desc", table_name="trades")
    op.drop_index("ix_trades_ts_id_desc", table_name="trades")
    op.drop_index("ix_snapshots_market_id_desc", table_name="market_snapshots")
    op.drop_index("ix_snapshots_market_ts_id_desc", table_name="market_snapshots")
