"""
NEXUS AI · tests/test_agent_monitor.py
Endpoints behind the Agent Monitor page: the live step of each active run
(read from the LangGraph checkpoint) and the model name (read from config).
The graph is replaced with a fake, so no LLM or Redis is needed.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

import app.api.v1.agents as agents_api
from app.main import app
from app.database import Base, get_async_db
from app.core.security import create_access_token, hash_password
from app.models.user import User, UserRole
from app.models.ticket import AssignedAgent, Ticket, TicketStatus
from rag.config import rag_settings

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
async def tickets():
    """An agent user and four tickets: three in progress, one resolved."""
    async with TestSessionLocal() as db:
        user = User(email="agent@nexus.ai", username="test_agent", role=UserRole.AGENT,
                    hashed_password=hash_password("testpass123"))
        db.add(user)
        await db.flush()
        rows = {}
        for num, status, agent in [
            ("NX-2026-00001", TicketStatus.IN_PROGRESS, AssignedAgent.TRIAGE),   # running diagnosis
            ("NX-2026-00002", TicketStatus.IN_PROGRESS, AssignedAgent.HUMAN),    # paused for review
            ("NX-2026-00003", TicketStatus.IN_PROGRESS, AssignedAgent.TRIAGE),   # not started yet
            ("NX-2026-00004", TicketStatus.RESOLVED,    AssignedAgent.TRIAGE),   # finished
        ]:
            t = Ticket(ticket_number=num, title=f"Ticket {num}", description="desc",
                       status=status, assigned_agent=agent, created_by_id=user.id)
            db.add(t)
            rows[num] = t
        await db.commit()
    token = create_access_token(subject=str(user.id), extra_claims={"role": "agent"})
    return rows, token


def fake_graph(states: dict):
    """states: ticket_id → (next tuple, values dict); missing ids have no checkpoint."""
    class Graph:
        async def aget_state(self, config):
            nxt, values = states.get(config["configurable"]["thread_id"], ((), {}))
            return SimpleNamespace(next=nxt, values=values)

    @asynccontextmanager
    async def _get():
        yield Graph()

    return _get


async def _get(token, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_active_runs_show_live_step_not_assigned_agent(tickets, monkeypatch):
    rows, token = tickets
    monkeypatch.setattr(agents_api, "get_supervisor_graph", fake_graph({
        str(rows["NX-2026-00001"].id): (("diagnosis_agent",), {"iteration": 1}),
        str(rows["NX-2026-00002"].id): (("human_review",), {"iteration": 2}),
    }))

    resp = await _get(token, "/api/v1/agents/active")

    assert resp.status_code == 200
    by_num = {i["ticket_number"]: i for i in resp.json()["items"]}
    assert set(by_num) == {"NX-2026-00001", "NX-2026-00002", "NX-2026-00003"}   # resolved one excluded
    assert by_num["NX-2026-00001"]["current_step"] == "Diagnosis"
    assert by_num["NX-2026-00001"]["current_node"] == "diagnosis_agent"
    assert by_num["NX-2026-00001"]["iteration"] == 1
    assert by_num["NX-2026-00002"]["current_step"] == "Awaiting human review"
    assert by_num["NX-2026-00003"]["current_step"] == "Queued"


@pytest.mark.asyncio
async def test_active_runs_still_listed_when_redis_is_down(tickets, monkeypatch):
    rows, token = tickets

    @asynccontextmanager
    async def broken():
        raise ConnectionError("redis down")
        yield

    monkeypatch.setattr(agents_api, "get_supervisor_graph", broken)
    resp = await _get(token, "/api/v1/agents/active")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    assert all(i["current_step"] == "Unknown" for i in items)


def test_live_step_after_graph_stopped():
    assert agents_api.live_step(None, True, AssignedAgent.HUMAN) == "With a human"
    assert agents_api.live_step(None, True, AssignedAgent.TRIAGE) == "Finishing"
    assert agents_api.live_step(None, False, AssignedAgent.TRIAGE) == "Queued"
    assert agents_api.live_step(("security_agent",), True, AssignedAgent.TRIAGE) == "Security"


@pytest.mark.asyncio
async def test_info_reports_configured_model(tickets):
    _, token = tickets
    resp = await _get(token, "/api/v1/agents/info")
    assert resp.status_code == 200
    assert resp.json()["llm_model"] == rag_settings.llm_model


async def _post(token, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, headers={"Authorization": f"Bearer {token}"})


async def _open_ticket(rows):
    """Put the finished ticket back to open, as if it had just been created."""
    ticket_id = rows["NX-2026-00004"].id
    async with TestSessionLocal() as db:
        t = await db.get(Ticket, ticket_id)
        t.status = TicketStatus.OPEN
        await db.commit()
    return ticket_id


@pytest.mark.asyncio
async def test_run_marks_ticket_in_progress_before_the_worker_starts(tickets, monkeypatch):
    """The click itself must flip the status, so the UI shows the run and a second click is refused."""
    import app.workers.celery_app as workers
    rows, token = tickets
    ticket_id = await _open_ticket(rows)
    queued = []
    monkeypatch.setattr(workers.process_ticket_task, "delay",
                        lambda tid: queued.append(tid) or SimpleNamespace(id="task-1"))

    first = await _post(token, f"/api/v1/agents/run/{ticket_id}")
    second = await _post(token, f"/api/v1/agents/run/{ticket_id}")

    assert first.status_code == 200
    assert second.status_code == 409
    assert queued == [str(ticket_id)]            # only one run dispatched
    async with TestSessionLocal() as db:
        t = await db.get(Ticket, ticket_id)
        assert t.status == TicketStatus.IN_PROGRESS
        assert t.assigned_agent == AssignedAgent.TRIAGE


@pytest.mark.asyncio
async def test_run_puts_ticket_back_when_it_cannot_be_queued(tickets, monkeypatch):
    import app.workers.celery_app as workers
    rows, token = tickets
    ticket_id = await _open_ticket(rows)

    def broker_down(_):
        raise ConnectionError("broker down")
    monkeypatch.setattr(workers.process_ticket_task, "delay", broker_down)

    resp = await _post(token, f"/api/v1/agents/run/{ticket_id}")

    assert resp.status_code == 503
    async with TestSessionLocal() as db:
        t = await db.get(Ticket, ticket_id)
        assert t.status == TicketStatus.OPEN
