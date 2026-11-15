"""
NEXUS AI · tests/test_ticket_search.py
Search (`q`) and paging on GET /tickets, as used by the Tickets screen.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.main import app
from app.database import Base, get_async_db
from app.core.security import create_access_token, hash_password
from app.models.user import User, UserRole
from app.models.ticket import Ticket, TicketStatus

TEST_DB_URL = "postgresql+asyncpg://nexus:nexus_secret@localhost:15432/nexusai_test"

test_engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    app.dependency_overrides[get_async_db] = override_get_db
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def data():
    """
    An agent, a plain user, and 25 tickets owned by the agent (NX-2026-00001..25,
    one per minute) plus one ticket owned by the plain user.
    """
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    async with TestSessionLocal() as db:
        agent = User(email="agent@nexus.ai", username="agent1", role=UserRole.AGENT,
                     hashed_password=hash_password("testpass123"))
        plain = User(email="user@nexus.ai", username="user1", role=UserRole.USER,
                     hashed_password=hash_password("testpass123"))
        db.add_all([agent, plain])
        await db.flush()
        for i in range(1, 26):
            db.add(Ticket(
                ticket_number=f"NX-2026-{i:05d}",
                title="Nginx returns 502" if i == 7 else f"Routine ticket {i}",
                description="Users see Bad Gateway from the edge proxy" if i == 12 else "Nothing special",
                status=TicketStatus.CLOSED if i == 7 else TicketStatus.OPEN,
                created_by_id=agent.id,
                created_at=base + timedelta(minutes=i),
            ))
        db.add(Ticket(ticket_number="NX-2026-00099", title="My laptop nginx config",
                      description="Personal ticket", status=TicketStatus.OPEN,
                      created_by_id=plain.id, created_at=base + timedelta(hours=1)))
        await db.commit()
    tok = lambda u, role: create_access_token(subject=str(u.id), extra_claims={"role": role})
    return {"agent": tok(agent, "agent"), "user": tok(plain, "user")}


async def _list(token, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/tickets/", params=params,
                                headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _numbers(body):
    return [t["ticket_number"] for t in body["items"]]


@pytest.mark.asyncio
async def test_search_by_ticket_number(data):
    body = await _list(data["agent"], q="nx-2026-00004")          # case-insensitive
    assert _numbers(body) == ["NX-2026-00004"]


@pytest.mark.asyncio
async def test_search_matches_title_and_description(data):
    assert _numbers(await _list(data["agent"], q="NGINX")) == ["NX-2026-00099", "NX-2026-00007"]
    assert _numbers(await _list(data["agent"], q="bad gateway")) == ["NX-2026-00012"]


@pytest.mark.asyncio
async def test_search_combines_with_status_filter(data):
    assert _numbers(await _list(data["agent"], q="nginx", status="closed")) == ["NX-2026-00007"]
    assert _numbers(await _list(data["agent"], q="nginx", status="open")) == ["NX-2026-00099"]


@pytest.mark.asyncio
async def test_plain_user_cannot_find_other_users_tickets(data):
    assert _numbers(await _list(data["user"], q="nginx")) == ["NX-2026-00099"]
    assert (await _list(data["user"], q="NX-2026-00007"))["total"] == 0


@pytest.mark.asyncio
async def test_like_wildcards_are_matched_literally(data):
    assert (await _list(data["agent"], q="%"))["total"] == 0
    assert (await _list(data["agent"], q="_"))["total"] == 0


@pytest.mark.asyncio
async def test_paging_returns_every_ticket_once_newest_first(data):
    p1 = await _list(data["agent"], page=1, page_size=20)
    p2 = await _list(data["agent"], page=2, page_size=20)
    assert p1["total"] == 26 and p1["pages"] == 2
    assert len(p1["items"]) == 20 and len(p2["items"]) == 6
    seen = _numbers(p1) + _numbers(p2)
    assert len(set(seen)) == 26
    assert seen[0] == "NX-2026-00099" and seen[-1] == "NX-2026-00001"
