"""
NEXUS AI · tests/test_auth.py
Self-registration must never let the caller choose a role; the first admin is
promoted with the `make-admin` CLI command.
"""

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

import app.database as app_database
from app.cli import make_admin
from app.main import app
from app.database import Base, get_async_db
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole

TEST_DB_URL = "postgresql+asyncpg://nexus:nexus_secret@localhost:15432/nexusai_test"

test_engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session
        await session.commit()


@asynccontextmanager
async def db_context_for_tests():
    async with TestSessionLocal() as session:
        yield session
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    app.dependency_overrides[get_async_db] = override_get_db
    monkeypatch.setattr(app_database, "get_db_context", db_context_for_tests)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _register(**extra):
    body = {"email": "new@nexus.ai", "username": "newbie", "password": "Passw0rd!x", **extra}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/v1/auth/register", json=body)


async def _role_of(username):
    async with TestSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        return user.role if user else None


@pytest.mark.asyncio
async def test_register_creates_plain_user():
    resp = await _register()
    assert resp.status_code == 201
    assert resp.json()["role"] == "user"
    assert await _role_of("newbie") == UserRole.USER


@pytest.mark.asyncio
async def test_register_rejects_role_field():
    resp = await _register(role="admin")
    assert resp.status_code == 422
    assert await _role_of("newbie") is None   # nothing was created


@pytest.mark.asyncio
async def test_make_admin_promotes_and_audits():
    await _register()
    assert await make_admin("newbie") == 0
    assert await _role_of("newbie") == UserRole.ADMIN
    async with TestSessionLocal() as db:
        logs = (await db.execute(select(AuditLog).where(AuditLog.action == "user.role_change"))).scalars().all()
    assert len(logs) == 1
    assert logs[0].details == {"old_role": "user", "new_role": "admin", "via": "cli"}


@pytest.mark.asyncio
async def test_make_admin_unknown_user_fails():
    assert await make_admin("ghost") == 1
