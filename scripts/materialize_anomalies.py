from app.db.session import SessionLocal
from app.services.anomaly_materializer import materialize_anomalies
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    new_run_id,
)


def main() -> None:
    run_id = new_run_id("anomalies")
    mark_pipeline_start(
        "quote_book_anomalies",
        detail="Starting standalone quote/book anomaly materializer.",
        run_id=run_id,
    )
    db = SessionLocal()
    try:
        result = materialize_anomalies(db)
        print(result)
    except Exception as exc:
        db.rollback()
        mark_pipeline_error(
            "quote_book_anomalies",
            exc,
            detail="Standalone quote/book anomaly materializer failed.",
            run_id=run_id,
        )
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
