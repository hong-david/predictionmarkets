from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Prediction Market Surveillance"
    app_env: str = "local"
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_private_key_pem: str = ""
    kalshi_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    # orderbook_delta requires explicit market tickers. If a non-empty list is
    # configured, those are used verbatim. Otherwise the consumer falls back to
    # the most recently updated active markets in the DB, capped by the limit.
    kalshi_book_market_tickers: list[str] = []
    kalshi_book_market_limit: int = 50
    kalshi_ws_queue_size: int = 10000
    kalshi_ws_worker_count: int = 4

    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "surveillance"
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379

    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_default_per_minute: int = 120
    rate_limit_expensive_per_minute: int = 30
    rate_limit_health_per_minute: int = 600

    clickhouse_url: str = "http://127.0.0.1:8123"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_database: str = "surveillance"
    clickhouse_batch_max_rows: int = 5000
    clickhouse_batch_flush_interval_sec: float = 1.0
    clickhouse_batch_queue_size: int = 100000
    kalshi_raw_backend: str = "postgres"  # postgres | clickhouse | dual

    opensearch_url: str = ""
    opensearch_index_prefix: str = "predictionmarkets"
    opensearch_timeout_sec: float = 2.0
    search_postgres_profile_limit: int = 12000
    news_user_agent: str = (
        "InformedPredictions/1.0 "
        "(public prediction-market surveillance; contact: admin@informedpredictions.com)"
    )

    # Hot raw retention defaults. These are intentionally short because the
    # durable artifact is a promoted evidence bundle, not every Kalshi tick.
    kalshi_raw_observe_ttl_hours: int = 1
    kalshi_raw_sampled_ttl_hours: int = 24
    kalshi_raw_hot_ttl_hours: int = 168
    kalshi_raw_triggered_ttl_hours: int = 720

    # Storage guardrails for dashboard health and maintenance scripts.
    storage_warning_used_ratio: float = 0.80
    storage_error_used_ratio: float = 0.90
    retention_book_events_max_age_days: int = 7
    retention_snapshot_observe_max_age_days: int = 2
    retention_snapshot_sampled_max_age_days: int = 7
    retention_snapshot_hot_max_age_days: int = 30
    retention_batch_size: int = 500

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def search_url(self) -> str:
        return self.opensearch_url or "http://127.0.0.1:9200"

    @property
    def search_index_prefix(self) -> str:
        return self.opensearch_index_prefix or "predictionmarkets"

    @property
    def search_timeout_sec(self) -> float:
        return float(self.opensearch_timeout_sec or 2.0)


settings = Settings()
