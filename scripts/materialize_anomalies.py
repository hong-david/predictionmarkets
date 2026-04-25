from app.db.session import SessionLocal
from app.services.anomaly_materializer import materialize_anomalies


def main() -> None:
    db = SessionLocal()
    try:
        result = materialize_anomalies(db)
        print(result)
    finally:
        db.close()


if __name__ == "__main__":
    main()