from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/archive-status")
async def archive_status() -> dict[str, bool | str | None]:
    return {
        "archive_mode": settings.archive_mode,
        "data_cutoff_at": settings.data_cutoff_at or None,
    }
