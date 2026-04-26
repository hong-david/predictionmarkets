from fastapi import FastAPI

from app.api.routes.anomalies import router as anomalies_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.features import router as features_router
from app.api.routes.health import router as health_router
from app.api.routes.markets import router as markets_router
from app.core.config import settings

app = FastAPI(title=settings.app_name)

app.include_router(health_router)
app.include_router(markets_router)
app.include_router(features_router)
app.include_router(anomalies_router)
app.include_router(dashboard_router)