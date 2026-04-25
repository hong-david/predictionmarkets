from app.db.models import Market
from app.db.session import SessionLocal


def main() -> None:
    db = SessionLocal()
    try:
        market = Market(
            platform="kalshi",
            market_id="test-market-1",
            event_id="test-event-1",
            ticker="TEST-001",
            title="Test Market",
            subtitle="Used to verify database inserts",
            status="open",
        )
        db.add(market)
        db.commit()
        db.refresh(market)
        print(f"Inserted market with id={market.id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()