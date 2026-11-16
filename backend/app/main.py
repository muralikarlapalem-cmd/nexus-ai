"""
NEXUS AI · app/main.py
FastAPI application entry point.
Wires up: lifespan (DB init), middleware (CORS, rate limit, request-ID, logging),
and all API v1 routers.
"""

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from app.api.v1 import auth, health, tickets, users, rag, agents
from app.config import settings
from app.database import close_db, init_db


# ── Rate limiter ──────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=[f"{settings.rate_limit_per_minute}/minute"])


# ── Lifespan ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup + shutdown lifecycle."""
    logger.info(f"Starting {settings.app_name} v{settings.app_version} [{settings.app_env}]")

    # Dev only: auto-create tables; prod uses Alembic
    if settings.is_development:
        await init_db()

    # Phase 2: ensure Qdrant collection exists on startup
    try:
        from rag.retrieval.vector_store import ensure_collection
        from rag.retrieval.bm25_store import get_bm25_store
        await ensure_collection()
        get_bm25_store()   # load BM25 index from disk if it exists
        logger.info("Qdrant collection ready")
    except Exception as e:
        logger.warning(f"Qdrant not reachable at startup (will retry on first use): {e}")

    # Load the reranker + embedder in the background, so the first
    # AI Knowledge Query doesn't wait ~10s for them
    try:
        from rag.warmup import start_warm_up
        start_warm_up()
    except ImportError as e:
        logger.warning(f"RAG warm-up skipped: {e}")

    yield

    await close_db()
    logger.info("Shutdown complete")


# ── App ───────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Multi-Agent IT Service Intelligence Platform",
    docs_url="/docs" if not settings.is_production else None,  # disable Swagger in prod
    redoc_url="/redoc" if not settings.is_production else None,
    lifespan=lifespan,
)

# ── Rate limiter middleware ────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ── CORS ──────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request logging + X-Request-ID ───────────────────────────
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id

    start = time.perf_counter()
    try:
        response: Response = await call_next(request)
    except Exception as exc:
        logger.exception(f"Unhandled error on {request.method} {request.url.path}")
        response = JSONResponse(status_code=500, content={"detail": "Internal server error"})

    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"

    logger.info(
        f"{request.method} {request.url.path} → {response.status_code} "
        f"({duration_ms:.1f}ms) [req={request_id}]"
    )
    return response


# ── Routers ───────────────────────────────────────────────────
API_PREFIX = "/api/v1"

app.include_router(health.router, prefix=API_PREFIX)
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(tickets.router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(rag.router, prefix=API_PREFIX)     # Phase 2
app.include_router(agents.router, prefix=API_PREFIX)  # Phase 3

# Phase 3+ routers (added as phases are built):
# from app.api.v1 import agents, analytics
# app.include_router(agents.router, prefix=API_PREFIX)
# app.include_router(analytics.router, prefix=API_PREFIX)


# ── Root ─────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/api/v1/health",
    }
