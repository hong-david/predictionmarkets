from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Prediction Market Surveillance"
    app_env: str = "local"
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
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

    clickhouse_url: str = "http://127.0.0.1:8123"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_database: str = "surveillance"
    clickhouse_batch_max_rows: int = 5000
    clickhouse_batch_flush_interval_sec: float = 1.0
    clickhouse_batch_queue_size: int = 100000
    kalshi_raw_backend: str = "postgres"  # postgres | clickhouse | dual

    # Hot raw retention defaults. These are intentionally short because the
    # durable artifact is a promoted evidence bundle, not every Kalshi tick.
    kalshi_raw_observe_ttl_hours: int = 1
    kalshi_raw_sampled_ttl_hours: int = 24
    kalshi_raw_hot_ttl_hours: int = 168
    kalshi_raw_triggered_ttl_hours: int = 720

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


settings = Settings()
