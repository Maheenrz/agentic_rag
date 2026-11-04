"""
main.py
-------
Application factory. Wires routers together, sets up CORS, exposes /health.

Business logic lives in:
  - app/routers/*          HTTP endpoints
  - app/deps.py            shared FastAPI dependencies
  - app/identity.py        password hashing + JWT
  - app/guardrails/*       injection + PII rules
  - app/stores/*           DB access
  - app/rag/*              retrieval + ingestion
  - app/core/*             LangGraph agent
  - app/services/cleanup*  durable document/collection vector cleanup
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import CORS_ORIGINS, logger
from app.identity import ACCESS_COOKIE_NAME
from app.db import init_db

from app.routers import auth as auth_router
from app.routers import me as me_router
from app.routers import org as org_router
from app.routers import collections as collections_router
from app.routers import threads as threads_router
from app.routers import guest as guest_router
from app.services.cleanup_worker import worker as cleanup_worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
    except Exception:
        logger.exception(
            "Database initialization failed at startup; the app will "
            "still start, but requests needing Postgres will fail."
        )

    cleanup_worker.start()
    try:
        yield
    finally:
        await cleanup_worker.stop()


app = FastAPI(title="Private Docs Assistant API", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    """Return validation errors without echoing submitted values."""
    safe_errors = []
    for error in exc.errors():
        safe_errors.append({
            "type": error.get("type", "validation_error"),
            "loc": list(error.get("loc", [])),
            "msg": error.get("msg", "Invalid value"),
        })
    return JSONResponse(status_code=422, content={"detail": safe_errors})


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Requested-With"],
)


@app.middleware("http")
async def browser_origin_guard(request: Request, call_next):
    """Reject cross-origin browser writes, including login-CSRF attempts.

    Browser fetches from the app include Origin on unsafe methods. Requests
    carrying an auth cookie without Origin are rejected; this avoids treating
    a missing Origin as a trusted browser request. Same-origin Vercel rewrites
    keep the browser cookie first-party while this guard validates the origin.
    """
    if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        origin = request.headers.get("origin")
        has_auth_cookie = bool(request.cookies.get(ACCESS_COOKIE_NAME))

        if origin and origin.rstrip("/") not in CORS_ORIGINS:
            return JSONResponse(
                status_code=403,
                content={"detail": "Untrusted request origin"},
            )

        if has_auth_cookie and not origin:
            return JSONResponse(
                status_code=403,
                content={"detail": "Origin header required"},
            )

    return await call_next(request)


@app.get("/health")
def health():
    return {"ok": True}


app.include_router(auth_router.router)
app.include_router(me_router.router)
app.include_router(org_router.router)
app.include_router(collections_router.router)
app.include_router(threads_router.router)
app.include_router(guest_router.router)