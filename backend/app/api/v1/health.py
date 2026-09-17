"""
NEXUS AI · app/api/v1/health.py
Health check endpoints — used by Docker, Kubernetes liveness/readiness probes.
GET /health        — basic liveness (always 200 if process is up)
GET /health/ready  — readiness (DB + Redis reachable)
GET /health/info   — version + environment info
"""

from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_async_db

router = APIRouter(prefix="/health", tags=["Health"])

_start_time = datetime.now(timezone.utc)


@router.get("/", summary="Liveness probe")
async def liveness():
    """Always returns 200 — confirms the process is running."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/ready", summary="Readiness probe")
async def readiness(db: AsyncSession = Depends(get_async_db)):
    """
    Checks: PostgreSQL, Redis.
    Returns 200 only when all dependencies are reachable.
    """
    checks: dict[str, str] = {}
    overall_ok = True

    # PostgreSQL
    try:
        await db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"
        overall_ok = False

    # Redis
    try:
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"
        overall_ok = False

    return {
        "status": "ready" if overall_ok else "degraded",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/info", summary="App info")
async def info():
    """Return application version and environment details."""
    uptime = datetime.now(timezone.utc) - _start_time
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "uptime_seconds": int(uptime.total_seconds()),
        "llm_model": settings.llm_model,
    }
