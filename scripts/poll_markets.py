import time
from datetime import datetime, timezone

from app.db.session import SessionLocal
from app.services.anomaly_materializer import materialize_anomalies
from app.services.kalshi_rest import KalshiRestClient
from app.services.market_ingestor import ingest_markets_payload

POLL_INTERVAL_SECONDS = 30
FETCH_LIMIT = 25
ANOMALY_MARKET_LIMIT = 100
ANOMALY_LOOKBACK = 5


def utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cycle() -> None:
    print(f"[{utc_now_str()}] starting poll cycle")

    client = KalshiRestClient()
    payload = client.get_markets(limit=FETCH_LIMIT)

    db = SessionLocal()
    try:
        ingest_result = ingest_markets_payload(db, payload)
        anomaly_result = materialize_anomalies(
            db,
            market_limit=ANOMALY_MARKET_LIMIT,
            lookback=ANOMALY_LOOKBACK,
        )

        print(
            f"[{utc_now_str()}] ingest={ingest_result} "
            f"anomalies={anomaly_result}"
        )
    finally:
        db.close()


def main() -> None:
    print(
        f"Starting poller: interval={POLL_INTERVAL_SECONDS}s, "
        f"fetch_limit={FETCH_LIMIT}"
    )

    try:
        while True:
            run_cycle()
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("Poller stopped.")


if __name__ == "__main__":
    main()