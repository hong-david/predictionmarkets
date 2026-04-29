"""FastAPI app composition root.

The HTTP surface has two halves:

  - **JSON API** (everything mounted by the routers below). Stable
    contract for the React frontend in `frontend/`.
  - **Static frontend** (`frontend/dist/`). Built by `npm run build`.
    In dev we don't serve it from FastAPI — Vite's dev server runs on
    :5173 and proxies `/api/*` here. In production we serve it via a
    catch-all route below.

Why a catch-all instead of `app.mount("/", StaticFiles(...))`?
  Mounting StaticFiles at `/` intercepts every request including
  `/api/*` paths *before* FastAPI can route them, because Starlette
  treats the mount as a sub-app at the root. A catch-all route
  registered last lets all the API routers match first and only
  serves static assets for paths nobody else handles.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.routes.anomalies import router as anomalies_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.features import router as features_router
from app.api.routes.health import router as health_router
from app.api.routes.markets import router as markets_router
from app.core.config import settings
from app.core.rate_limit import RateLimiter

app = FastAPI(title=settings.app_name)
_rate_limiter = RateLimiter.from_settings(settings)


@app.middleware("http")
async def rate_limit_api_requests(request, call_next):
    decision = await run_in_threadpool(_rate_limiter.check_request, request)
    if decision is not None and not decision.allowed:
        return JSONResponse(
            {
                "detail": "Rate limit exceeded",
                "limit": decision.rule.requests,
                "window_seconds": decision.rule.window_seconds,
                "rule": decision.rule.name,
            },
            status_code=429,
            headers={
                "Retry-After": str(decision.reset_seconds),
                "X-RateLimit-Limit": str(decision.rule.requests),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(decision.reset_seconds),
            },
        )

    response = await call_next(request)
    if decision is not None:
        response.headers["X-RateLimit-Limit"] = str(decision.rule.requests)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        response.headers["X-RateLimit-Reset"] = str(decision.reset_seconds)
    return response

# The React app’s canonical read API is /api/dashboard/* (see dashboard_router).
# Other routers below stay on /api/* for scripts and old tests; they are
# marked deprecated in OpenAPI — prefer the dashboard path for new clients.
app.include_router(health_router, prefix="/api")
app.include_router(markets_router, prefix="/api")
app.include_router(features_router, prefix="/api")
app.include_router(anomalies_router, prefix="/api")
app.include_router(dashboard_router)


_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
_FRONTEND_INDEX = _FRONTEND_DIST / "index.html"

_PLACEHOLDER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Prediction Market Surveillance</title>
<style>
body{margin:0;background:#0b0d10;color:#e6e8eb;font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;}
main{max-width:640px;margin:8% auto;padding:0 24px;}
h1{font-size:22px;margin:0 0 12px}
p{color:#8a93a0}
code{background:#1b2027;padding:2px 6px;border-radius:3px;color:#e6e8eb;font-family:SF Mono,Consolas,monospace;font-size:13px}
a{color:#4ea1ff}
pre{background:#14181d;padding:12px 16px;border-radius:6px;border:1px solid #232932;overflow:auto}
</style></head><body><main>
<h1>Frontend not built</h1>
<p>The React dashboard is in <code>frontend/</code> and hasn't been built yet.</p>
<p>For development:</p>
<pre><code>cd frontend
npm install
npm run dev</code></pre>
<p>Then visit <a href="http://localhost:5173">http://localhost:5173</a> &mdash; the dev server proxies <code>/api/*</code> to this FastAPI process.</p>
<p>For production:</p>
<pre><code>cd frontend
npm run build</code></pre>
<p>and reload uvicorn. JSON API browsable at <a href="/docs">/docs</a>.</p>
</main></body></html>"""


# These prefixes belong to FastAPI itself (or to its auto-generated
# docs). The catch-all below explicitly skips them so an unknown
# `/api/...` path returns a proper JSON 404 from FastAPI instead of
# being absorbed into the SPA fallback.
_RESERVED_PATHS = ("api", "docs", "redoc", "openapi.json")


@app.get("/{full_path:path}", include_in_schema=False, response_model=None)
def serve_frontend(full_path: str):
    head = full_path.split("/", 1)[0]
    if head in _RESERVED_PATHS:
        raise HTTPException(status_code=404, detail="Not Found")

    if not _FRONTEND_INDEX.exists():
        return HTMLResponse(_PLACEHOLDER_HTML)

    # Static asset (JS / CSS / images). Resolve under dist/ to defend
    # against `..` traversal — `relative_to` raises if the resolved
    # path escapes the dist directory.
    if full_path:
        candidate = (_FRONTEND_DIST / full_path).resolve()
        try:
            candidate.relative_to(_FRONTEND_DIST.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="Not Found")
        if candidate.is_file():
            return FileResponse(candidate)

    # SPA fallback: any unknown path serves index.html so React Router
    # can render its own 404 (or the requested route).
    return FileResponse(_FRONTEND_INDEX)
