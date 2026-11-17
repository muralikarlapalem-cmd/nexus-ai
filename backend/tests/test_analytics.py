"""
NEXUS AI · tests/test_analytics.py
GET /analytics/summary: period counts, the SLA rule (judged from timestamps,
not the sla_breached flag), the median resolve time, and who may call it.
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
from app.models.ticket import Ticket, TicketCategory, TicketPriority, TicketStatus

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


async def _user(role: UserRole) -> tuple[User, str]:
    async with TestSessionLocal() as db:
        user = User(email=f"{role.value}@nexus.ai", username=f"test_{role.value}", role=role,
                    hashed_password=hash_password("testpass123"))
        db.add(user)
        await db.commit()
    return user, create_access_token(subject=str(user.id), extra_claims={"role": role.value})


async def _add(user, rows):
    """rows: dicts of Ticket fields; created_at / resolved_at / sla_due_at are hours relative to now."""
    now = datetime.now(timezone.utc)
    async with TestSessionLocal() as db:
        for i, row in enumerate(rows, start=1):
            row = dict(row)
            for field in ("created_at", "resolved_at", "sla_due_at"):
                if row.get(field) is not None:
                    row[field] = now + timedelta(hours=row[field])
            db.add(Ticket(ticket_number=f"NX-2026-{i:05d}", title=f"Ticket {i}", description="desc",
                          created_by_id=user.id, **row))
        await db.commit()


async def _summary(token, query=""):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(f"/api/v1/analytics/summary{query}",
                                headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_summary_counts_the_period_and_the_one_before():
    user, token = await _user(UserRole.AGENT)
    await _add(user, [
        # this period (last 7 days)
        dict(created_at=-30, status=TicketStatus.OPEN, category=TicketCategory.NETWORK),
        dict(created_at=-50, status=TicketStatus.IN_PROGRESS, category=TicketCategory.NETWORK),
        dict(created_at=-60, resolved_at=-56, status=TicketStatus.RESOLVED, category=TicketCategory.ACCESS),
        dict(created_at=-60, resolved_at=-50, status=TicketStatus.RESOLVED, category=TicketCategory.ACCESS),
        dict(created_at=-60, resolved_at=-40, status=TicketStatus.RESOLVED, category=TicketCategory.ACCESS),
        # the period before: created and resolved 10 days ago
        dict(created_at=-240, resolved_at=-238, status=TicketStatus.RESOLVED),
        # older than both periods
        dict(created_at=-24 * 40, resolved_at=-24 * 39, status=TicketStatus.RESOLVED),
    ])

    resp = await _summary(token, "?days=7")

    assert resp.status_code == 200
    data = resp.json()
    assert data["current"]["created"] == 5
    assert data["current"]["resolved"] == 3
    assert data["current"]["median_resolve_hours"] == 10.0      # 4 h, 10 h, 20 h
    assert data["previous"]["created"] == 1
    assert data["previous"]["resolved"] == 1
    assert data["previous"]["median_resolve_hours"] == 2.0
    assert data["open_backlog"] == 2

    assert len(data["trend"]) == 7
    assert data["trend"][-1]["date"] == data["period"]["end"]
    assert sum(d["created"] for d in data["trend"]) == 5
    assert sum(d["resolved"] for d in data["trend"]) == 3

    categories = {c["category"]: c["count"] for c in data["by_category"]}
    assert categories["access"] == 3 and categories["network"] == 2 and categories["hardware"] == 0
    assert data["by_category"][0]["category"] == "access"       # largest first


@pytest.mark.asyncio
async def test_sla_is_judged_from_timestamps_not_the_breached_flag():
    user, token = await _user(UserRole.ADMIN)
    high, low = TicketPriority.HIGH, TicketPriority.LOW
    await _add(user, [
        # resolved before the deadline: met
        dict(created_at=-10, resolved_at=-8, sla_due_at=-2, status=TicketStatus.RESOLVED, priority=high),
        # resolved after the deadline, flag never set by the sweep: still breached
        dict(created_at=-10, resolved_at=-1, sla_due_at=-2, status=TicketStatus.RESOLVED, priority=high,
             sla_breached=False),
        # open and past the deadline: breached
        dict(created_at=-10, sla_due_at=-2, status=TicketStatus.IN_PROGRESS, priority=high),
        # open, not yet due: not judged
        dict(created_at=-10, sla_due_at=5, status=TicketStatus.OPEN, priority=low),
        # cancelled after the deadline: not judged
        dict(created_at=-10, sla_due_at=-2, status=TicketStatus.CANCELLED, priority=low),
    ])

    data = (await _summary(token, "?days=7")).json()

    assert data["current"]["sla_met"] == 1
    assert data["current"]["sla_breached"] == 2
    assert data["current"]["sla_compliance_pct"] == 33.3
    assert data["open_overdue"] == 1

    by_priority = {p["priority"]: p for p in data["sla_by_priority"]}
    assert [p["priority"] for p in data["sla_by_priority"]] == ["critical", "high", "medium", "low"]
    assert (by_priority["high"]["met"], by_priority["high"]["breached"]) == (1, 2)
    assert by_priority["low"]["compliance_pct"] is None         # nothing judged yet
    assert by_priority["critical"]["compliance_pct"] is None


@pytest.mark.asyncio
async def test_summary_with_no_tickets():
    _, token = await _user(UserRole.ANALYST)

    resp = await _summary(token)

    assert resp.status_code == 200
    data = resp.json()
    assert data["period"]["days"] == 30
    assert data["current"] == {"created": 0, "resolved": 0, "sla_met": 0, "sla_breached": 0,
                               "sla_compliance_pct": None, "median_resolve_hours": None}
    assert data["open_backlog"] == 0
    assert len(data["trend"]) == 30
    assert all(d["created"] == 0 and d["resolved"] == 0 for d in data["trend"])


@pytest.mark.asyncio
async def test_days_are_cut_in_the_callers_time_zone():
    user, token = await _user(UserRole.AGENT)
    await _add(user, [dict(created_at=0, status=TicketStatus.OPEN)])

    # Kiritimati is UTC+14 and Niue is UTC-11: "today" is never the same date in both
    east = (await _summary(token, "?days=1&tz=Pacific/Kiritimati")).json()
    west = (await _summary(token, "?days=1&tz=Pacific/Niue")).json()

    assert east["period"]["end"] != west["period"]["end"]
    assert east["current"]["created"] == 1 and west["current"]["created"] == 1


@pytest.mark.asyncio
async def test_unknown_time_zone_is_rejected():
    _, token = await _user(UserRole.AGENT)
    resp = await _summary(token, "?tz=Mars/Olympus")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_regular_users_cannot_read_analytics():
    _, token = await _user(UserRole.USER)
    resp = await _summary(token)
    assert resp.status_code == 403
