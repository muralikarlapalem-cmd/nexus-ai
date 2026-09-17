"""
NEXUS AI · tests/test_tickets.py
Integration tests for the Ticket API using pytest-asyncio + httpx AsyncClient.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.main import app
from app.database import Base, get_async_db
from app.core.security import create_access_token, hash_password
from app.models.user import User, UserRole
from app.models.ticket import Ticket

# ── Test DB setup ─────────────────────────────────────────────
TEST_DB_URL = "postgresql+asyncpg://nexus:nexus_secret@localhost:15432/nexusai_test"

test_engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session
        await session.commit()

app.dependency_overrides[get_async_db] = override_get_db


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def test_user(setup_db):
    """Create an agent user and return their auth token."""
    async with TestSessionLocal() as db:
        user = User(
            email="agent@nexus.ai",
            username="test_agent",
            full_name="Test Agent",
            role=UserRole.AGENT,
            hashed_password=hash_password("testpass123"),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    token = create_access_token(
        subject=str(user.id),
        extra_claims={"role": UserRole.AGENT.value, "email": user.email},
    )
    return user, token


@pytest.mark.asyncio
async def test_create_ticket(test_user):
    user, token = test_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/tickets/",
            json={
                "title": "Server unreachable after maintenance",
                "description": "The production API server has been unreachable since 14:00 IST. Pings time out.",
                "category": "network",
                "priority": "high",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Server unreachable after maintenance"
    assert data["status"] == "open"
    assert data["ticket_number"].startswith("NX-")
    assert data["sla_breached"] is False


@pytest.mark.asyncio
async def test_list_tickets_pagination(test_user):
    user, token = test_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create 3 tickets
        for i in range(3):
            await client.post(
                "/api/v1/tickets/",
                json={"title": f"Issue {i}", "description": "Description " * 3, "priority": "low"},
                headers={"Authorization": f"Bearer {token}"},
            )
        # List with pagination
        response = await client.get(
            "/api/v1/tickets/?page=1&page_size=2",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert data["pages"] == 2


@pytest.mark.asyncio
async def test_change_ticket_status(test_user):
    user, token = test_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create
        create_resp = await client.post(
            "/api/v1/tickets/",
            json={"title": "Disk full on DB server", "description": "Disk usage at 99% on pg-prod-01", "priority": "critical"},
            headers={"Authorization": f"Bearer {token}"},
        )
        ticket_id = create_resp.json()["id"]

        # Change status
        status_resp = await client.post(
            f"/api/v1/tickets/{ticket_id}/status",
            json={"status": "resolved", "note": "Cleared old WAL logs, usage down to 40%"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "resolved"
    assert status_resp.json()["resolved_at"] is not None


@pytest.mark.asyncio
async def test_health_endpoints():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        liveness = await client.get("/api/v1/health/")
        info = await client.get("/api/v1/health/info")
    assert liveness.status_code == 200
    assert liveness.json()["status"] == "ok"
    assert info.json()["app"] == "NEXUS AI"
