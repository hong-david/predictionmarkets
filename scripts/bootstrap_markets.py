from app.db.session import SessionLocal
from app.services.kalshi_rest import KalshiRestClient
from app.services.market_ingestor import ingest_markets_payload


def main() -> None:
    client = KalshiRestClient()
    payload = client.get_markets(limit=25)

    db = SessionLocal()
    try:
        result = ingest_markets_payload(db, payload)
        print(result)
    finally:
        db.close()


if __name__ == "__main__":
    main()