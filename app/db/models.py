from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    BigInteger,
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(50), index=True)
    market_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    ticker: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    subtitle: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="open")
    open_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    close_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Classifier output (see app/services/classifier/). Populated lazily by
    # the layered classifier on every ingest. `classifier_version` lets a
    # bump in the rule set trigger a reclassification sweep without touching
    # rows that are already at the current version.
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    subcategory: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    manipulability_prior: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True
    )
    classifier_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    classifier_layer: Mapped[str | None] = mapped_column(String(32), nullable=True)
    classifier_rule: Mapped[str | None] = mapped_column(String(128), nullable=True)
    classifier_confidence: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True
    )
    classifier_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    snapshots: Mapped[list["MarketSnapshot"]] = relationship(
        back_populates="market",
        cascade="all, delete-orphan",
    )

    trades: Mapped[list["Trade"]] = relationship(
        back_populates="market",
        cascade="all, delete-orphan",
    )

    book_events: Mapped[list["BookEvent"]] = relationship(
        back_populates="market",
        cascade="all, delete-orphan",
    )

    metrics: Mapped["MarketMetric"] = relationship(
        back_populates="market",
        cascade="all, delete-orphan",
        uselist=False,
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_pk: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    last_price_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    yes_bid_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    yes_ask_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    no_bid_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    no_ask_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    volume_fp: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    volume_24h_fp: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    open_interest_fp: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    liquidity_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )

    market: Mapped["Market"] = relationship(back_populates="snapshots")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_pk: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    trade_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    yes_price_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    no_price_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    count_fp: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    taker_side: Mapped[str | None] = mapped_column(String(3), nullable=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    market: Mapped["Market"] = relationship(back_populates="trades")

    __table_args__ = (Index("ix_trades_market_pk_ts", "market_pk", "ts"),)


class BookEvent(Base):
    __tablename__ = "book_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_pk: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)

    # session_id is generated per WS connection. Kalshi's `seq` resets on
    # reconnect, so it is only meaningful within a single connection lifetime.
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer)

    # Event time from the message's ts_ms. Snapshots may not carry one, hence
    # nullable. received_at is always populated for ingest-time / clock-skew.
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    side: Mapped[str] = mapped_column(String(3))
    price_dollars: Mapped[Decimal] = mapped_column(Numeric(12, 4))

    # Snapshot rows carry the absolute level size; delta rows carry the signed
    # delta (positive adds contracts, negative removes, zero removes the level).
    size_fp: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    delta_fp: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)

    is_snapshot: Mapped[bool] = mapped_column(Boolean)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    market: Mapped["Market"] = relationship(back_populates="book_events")

    __table_args__ = (
        # Idempotency: re-applying the same Kalshi message (snapshot expansion
        # or single delta) is a no-op via ON CONFLICT DO NOTHING.
        UniqueConstraint(
            "session_id",
            "seq",
            "side",
            "price_dollars",
            name="uq_book_events_session_seq_side_price",
        ),
        # Replay queries: "give me events for market M in arrival order."
        Index("ix_book_events_market_pk_id", "market_pk", "id"),
    )


class Anomaly(Base):
    __tablename__ = "anomalies"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_pk: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    latest_snapshot_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    score: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    severity: Mapped[str] = mapped_column(String(20), index=True)
    reasons: Mapped[list] = mapped_column(JSON)
    signals: Mapped[dict] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    market: Mapped["Market"] = relationship()


class MarketMetric(Base):
    __tablename__ = "market_metrics"

    market_pk: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    latest_snapshot_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    latest_snapshot_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    last_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yes_bid_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yes_ask_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    no_bid_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    no_ask_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume_24h_contracts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    open_interest_contracts: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    liquidity_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trade_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    anomaly_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    high_anomaly_count: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    last_trade_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_anomaly_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    urgency_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    evidence_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    storage_tier: Mapped[str] = mapped_column(
        String(24), default="observe_only", nullable=False
    )
    retention_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retention_reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    market: Mapped["Market"] = relationship(back_populates="metrics")

    __table_args__ = (
        Index("ix_market_metrics_urgency_market", "urgency_score", "market_pk"),
        Index("ix_market_metrics_tier_updated", "storage_tier", "updated_at"),
    )


class MarketFeature1m(Base):
    __tablename__ = "market_features_1m"

    market_pk: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), primary_key=True
    )
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    trade_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trade_volume_contracts: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    max_trade_size_contracts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    first_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quote_updates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_spread_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_volume_contracts: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    add_volume_contracts: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    depth_pull_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    __table_args__ = (Index("ix_market_features_1m_bucket", "bucket_start"),)


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    canonical_url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_tier: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    entities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_vector_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_body_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class MarketNewsProfile(Base):
    __tablename__ = "market_news_profiles"

    market_pk: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    normalized_keywords: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False
    )
    entities: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    active_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    active_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    embedding_vector_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NewsEvent(Base):
    __tablename__ = "news_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("news_articles.id", ondelete="CASCADE"), index=True
    )
    market_pk: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), index=True
    )
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pre_news_trade_score: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    leakage_window_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default="candidate", nullable=False, index=True
    )
    score_components: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "article_id", "market_pk", name="uq_news_events_article_market"
        ),
        Index("ix_news_events_market_score", "market_pk", "pre_news_trade_score"),
    )


class CaseEvidence(Base):
    __tablename__ = "case_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    market_pk: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), index=True
    )
    trigger_kind: Mapped[str] = mapped_column(String(32), index=True)
    trigger_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    storage_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    classifier_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
