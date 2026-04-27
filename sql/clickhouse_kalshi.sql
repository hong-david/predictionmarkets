CREATE DATABASE IF NOT EXISTS surveillance;

CREATE TABLE IF NOT EXISTS surveillance.kalshi_trades_raw
(
    ts DateTime64(3),
    received_at DateTime64(3) DEFAULT now64(3),
    market_pk UInt64,
    trade_id String,
    yes_price_cents UInt8,
    no_price_cents UInt8,
    count_contracts UInt32,
    taker_side Enum8('unknown' = 0, 'yes' = 1, 'no' = 2),
    storage_tier LowCardinality(String),
    anomaly_context UInt8 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (market_pk, ts, trade_id)
TTL ts + INTERVAL 7 DAY DELETE;

CREATE TABLE IF NOT EXISTS surveillance.kalshi_quote_changes_raw
(
    ts DateTime64(3),
    received_at DateTime64(3) DEFAULT now64(3),
    market_pk UInt64,
    last_price_cents UInt8,
    yes_bid_cents UInt8,
    yes_ask_cents UInt8,
    no_bid_cents UInt8,
    no_ask_cents UInt8,
    volume_24h_contracts UInt64,
    open_interest_contracts UInt64,
    storage_tier LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (market_pk, ts)
TTL ts + INTERVAL 3 DAY DELETE;

CREATE TABLE IF NOT EXISTS surveillance.kalshi_l2_events_raw
(
    ts DateTime64(3),
    received_at DateTime64(3) DEFAULT now64(3),
    market_pk UInt64,
    session_id String,
    seq UInt64,
    side Enum8('yes' = 1, 'no' = 2),
    price_cents UInt8,
    size_contracts Nullable(UInt64),
    delta_contracts Nullable(Int64),
    is_snapshot UInt8,
    storage_tier LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (market_pk, ts, session_id, seq)
TTL ts + INTERVAL 1 DAY DELETE;

CREATE TABLE IF NOT EXISTS surveillance.market_features_1m
(
    bucket_start DateTime,
    market_pk UInt64,
    trade_count UInt32,
    trade_volume UInt64,
    max_trade_size UInt32,
    first_price_cents UInt8,
    last_price_cents UInt8,
    min_price_cents UInt8,
    max_price_cents UInt8,
    quote_updates UInt32,
    max_spread_cents UInt8,
    cancel_volume UInt64,
    add_volume UInt64,
    depth_pull_score Float32
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(bucket_start)
ORDER BY (market_pk, bucket_start);
