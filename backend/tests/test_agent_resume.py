"""
NEXUS AI · tests/test_agent_resume.py
Tests that resuming a run after human review saves the outcome to the ticket.
The LangGraph graph is replaced with a fake, so no LLM, Redis or Qdrant is needed.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

import agents.supervisor as supervisor
import app.database as app_database
from app.main import app
from app.database import Base, get_async_db
from app.core.security import create_access_token, hash_password
from app.models.user import User, UserRole
from app.models.ticket import AssignedAgent, Ticket, TicketStatus

TEST_DB_URL = "postgresql+asyncpg://nexus:nexus_secret@localhost:15432/nexusai_test"

test_engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session
        await session.commit()


@asynccontextmanager
async def db_context_for_tests():
    """Stands in for app.database.get_db_context, which the supervisor uses."""
    async with TestSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    app.dependency_overrides[get_async_db] = override_get_db
    monkeypatch.setattr(app_database, "get_db_context", db_context_for_tests)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def paused_ticket():
    """An agent user plus a ticket left in_progress by a run paused for review."""
    async with TestSessionLocal() as db:
        user = User(
            email="agent@nexus.ai", username="test_agent", role=UserRole.AGENT,
            hashed_password=hash_password("testpass123"),
        )
        db.add(user)
        await db.flush()
        ticket = Ticket(
            ticket_number="NX-2026-00001",
            title="Database connections refused",
            description="pg-prod-01 refuses new connections since 09:00",
            status=TicketStatus.IN_PROGRESS,
            assigned_agent=AssignedAgent.TRIAGE,
            created_by_id=user.id,
        )
        db.add(ticket)
        await db.commit()
        await db.refresh(user)
        await db.refresh(ticket)
    token = create_access_token(subject=str(user.id), extra_claims={"role": "agent"})
    return ticket, token


def fake_graph(final_state: dict, paused: bool = True):
    """Build a replacement for get_supervisor_graph returning a canned final state."""
    calls = {}

    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(next=("human_review",) if paused else ())

        async def ainvoke(self, command, config):
            calls["resume"] = command.resume
            return final_state

    @asynccontextmanager
    async def _get():
        yield Graph()

    return _get, calls


DIAGNOSIS = {"root_cause": "max_connections reached", "confidence": 0.8, "resolution_steps": []}


async def _resume(token, ticket_id, **body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            f"/api/v1/agents/resume/{ticket_id}",
            json={"approved": True, "notes": "", **body},
            headers={"Authorization": f"Bearer {token}"},
        )


async def _load(ticket_id):
    async with TestSessionLocal() as db:
        return await db.get(Ticket, ticket_id)


@pytest.mark.asyncio
async def test_approved_resume_resolves_ticket(paused_ticket, monkeypatch):
    ticket, token = paused_ticket
    graph, calls = fake_graph({
        "triage_result": {"priority": "high"},
        "diagnosis_result": DIAGNOSIS,
        "final_resolution": "[HUMAN REVIEW] ok to restart\n\nRoot Cause: max_connections reached",
        "hitl_required": False,
        "error": None,
    })
    monkeypatch.setattr(supervisor, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id, notes="ok to restart")

    assert resp.status_code == 200
    assert resp.json()["status"] == "resumed"
    assert calls["resume"]["approved"] is True
    saved = await _load(ticket.id)
    assert saved.status == TicketStatus.RESOLVED
    assert saved.resolved_at is not None
    assert "ok to restart" in saved.resolution_notes
    assert saved.diagnosis_output["root_cause"] == "max_connections reached"
    assert saved.triage_output == {"priority": "high"}


@pytest.mark.asyncio
async def test_rejected_resume_saves_but_does_not_resolve(paused_ticket, monkeypatch):
    ticket, token = paused_ticket
    graph, _ = fake_graph({
        "diagnosis_result": DIAGNOSIS,
        "final_resolution": "[HUMAN REVIEW] escalate to DBA team\n\nRoot Cause: max_connections reached",
        "hitl_required": False,
        "next": "END",
    })
    monkeypatch.setattr(supervisor, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id, approved=False, notes="escalate to DBA team")

    assert resp.status_code == 200
    saved = await _load(ticket.id)
    assert saved.status == TicketStatus.IN_PROGRESS
    assert saved.resolved_at is None
    assert saved.assigned_agent == AssignedAgent.HUMAN
    assert "escalate to DBA team" in saved.resolution_notes


@pytest.mark.asyncio
async def test_resume_that_pauses_again_stays_with_human(paused_ticket, monkeypatch):
    ticket, token = paused_ticket
    graph, _ = fake_graph({
        "diagnosis_result": DIAGNOSIS,
        "final_resolution": "Root Cause: max_connections reached",
        "hitl_required": True,
        "__interrupt__": [object()],
    })
    monkeypatch.setattr(supervisor, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id)

    assert resp.json()["status"] == "paused"
    saved = await _load(ticket.id)
    assert saved.status == TicketStatus.IN_PROGRESS
    assert saved.assigned_agent == AssignedAgent.HUMAN
    assert saved.diagnosis_output["root_cause"] == "max_connections reached"


@pytest.mark.asyncio
async def test_resume_without_paused_run_returns_409(paused_ticket, monkeypatch):
    ticket, token = paused_ticket
    graph, calls = fake_graph({}, paused=False)
    monkeypatch.setattr(supervisor, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id)

    assert resp.status_code == 409
    assert "resume" not in calls          # graph was not invoked
    saved = await _load(ticket.id)
    assert saved.status == TicketStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_guard_error_is_not_auto_resolved(paused_ticket):
    ticket, _ = paused_ticket
    await supervisor.persist_final_state(str(ticket.id), {
        "final_resolution": "Root Cause: unclear",
        "hitl_required": False,
        "error": "Max iterations (8) reached. Manual review required.",
    })
    saved = await _load(ticket.id)
    assert saved.status == TicketStatus.IN_PROGRESS
    assert saved.assigned_agent == AssignedAgent.HUMAN
    assert saved.resolution_notes == "Root Cause: unclear"
