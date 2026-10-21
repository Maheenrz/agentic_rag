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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import CORS_ORIGINS, logger
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
    # init_db() creates the pgvector extension and LangGraph checkpoint
    # tables; it was previously only ever called manually (tests, or
    # `python -m app.db`), so a fresh production database would otherwise
    # never get them. Every app-specific table (collections, documents,
    # cleanup_jobs, ...) is already created at import time by each store
    # module's own _init_db(), so this only needs to cover what's left.
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True}


app.include_router(auth_router.router)
app.include_router(me_router.router)
app.include_router(org_router.router)
app.include_router(collections_router.router)
app.include_router(threads_router.router)
app.include_router(guest_router.router)