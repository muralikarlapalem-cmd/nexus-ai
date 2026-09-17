"""
NEXUS AI · app/database.py
Async SQLAlchemy engine + session factory.
All models inherit from `Base`. Use `get_async_db` as a FastAPI dependency.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


# ── Engine ────────────────────────────────────────────────────
engine = create_async_engine(
    settings.database_url,
    echo=settings.is_development,       # log SQL in dev
    pool_pre_ping=True,                 # recycle stale connections
    pool_size=10,
    max_overflow=20,
    pool_recycle=3600,
)

# ── Session factory ───────────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,             # keep objects usable after commit
    autocommit=False,
    autoflush=False,
)


# ── Declarative base ──────────────────────────────────────────
class Base(DeclarativeBase):
    """All ORM models inherit from this."""
    pass


# ── FastAPI dependency ────────────────────────────────────────
async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield an async DB session and handle commit/rollback automatically.
    Use as: `db: AsyncSession = Depends(get_async_db)`
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Context manager for non-FastAPI code (workers, scripts) ──
@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """For use outside FastAPI request context (Celery tasks, migrations, etc.)"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Startup / Shutdown helpers ────────────────────────────────
async def init_db() -> None:
    """Create all tables (used in dev; prod uses Alembic migrations)."""
    from app.models import ticket, user, audit_log  # noqa: F401 — ensure models registered
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialised")


async def close_db() -> None:
    await engine.dispose()
    logger.info("Database connection pool closed")
