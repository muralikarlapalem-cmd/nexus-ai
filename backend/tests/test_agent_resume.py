"""
NEXUS AI · tests/test_agent_resume.py
Human review of a paused run: the endpoint queues the review for the worker,
and resuming the run saves the outcome to the ticket.
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
import app.api.v1.agents as agents_api
import app.workers.celery_app as workers
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
            assigned_agent=AssignedAgent.HUMAN,     # a paused run leaves the ticket with a human
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


def _review(approved=True, notes="", next_agent="diagnosis_agent"):
    return {"approved": approved, "next_agent": next_agent, "notes": notes}


@pytest.fixture
def queued(monkeypatch):
    """Capture reviews handed to the worker instead of sending them to Celery."""
    sent = []
    monkeypatch.setattr(workers.resume_ticket_task, "delay",
                        lambda tid, response: sent.append((tid, response)) or SimpleNamespace(id="task-1"))
    return sent


# ── The endpoint: validate, hand the ticket back to the agents, queue the review ──

@pytest.mark.asyncio
async def test_review_is_queued_for_the_worker(paused_ticket, queued, monkeypatch):
    ticket, token = paused_ticket
    graph, calls = fake_graph({})
    monkeypatch.setattr(agents_api, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id, notes="ok to restart", next_agent="security_agent")

    assert resp.status_code == 200
    assert resp.json()["status"] == "dispatched"
    assert queued == [(str(ticket.id), _review(notes="ok to restart", next_agent="security_agent"))]
    assert "resume" not in calls                    # the graph runs on the worker, not in the request
    saved = await _load(ticket.id)
    assert saved.assigned_agent == AssignedAgent.SECURITY


@pytest.mark.asyncio
async def test_second_review_for_the_same_pause_is_refused(paused_ticket, queued, monkeypatch):
    ticket, token = paused_ticket
    graph, _ = fake_graph({})                       # still paused: the worker has not picked it up yet
    monkeypatch.setattr(agents_api, "get_supervisor_graph", graph)

    first = await _resume(token, ticket.id)
    second = await _resume(token, ticket.id)

    assert (first.status_code, second.status_code) == (200, 409)
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_resume_without_paused_run_returns_409(paused_ticket, queued, monkeypatch):
    ticket, token = paused_ticket
    graph, _ = fake_graph({}, paused=False)
    monkeypatch.setattr(agents_api, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id)

    assert resp.status_code == 409
    assert queued == []
    saved = await _load(ticket.id)
    assert saved.status == TicketStatus.IN_PROGRESS
    assert saved.assigned_agent == AssignedAgent.HUMAN


@pytest.mark.asyncio
async def test_unknown_next_agent_is_rejected(paused_ticket, queued, monkeypatch):
    ticket, token = paused_ticket
    graph, _ = fake_graph({})
    monkeypatch.setattr(agents_api, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id, next_agent="no_such_agent")

    assert resp.status_code == 422
    assert queued == []


@pytest.mark.asyncio
async def test_approve_resolves_ticket_with_the_review_notes(paused_ticket, queued, monkeypatch):
    """Approve with next_agent=END: no agent runs, the notes become the resolution."""
    ticket, token = paused_ticket
    graph, calls = fake_graph({
        "final_resolution": "[HUMAN REVIEW] checked, safe to close\n\n",
        "hitl_required": False,
        "next": "END",
        "error": None,
    })
    monkeypatch.setattr(agents_api, "get_supervisor_graph", graph)
    monkeypatch.setattr(supervisor, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id, next_agent="END", notes="checked, safe to close")

    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert calls["resume"] == _review(notes="checked, safe to close", next_agent="END")
    assert queued == []                             # nothing sent to the worker
    saved = await _load(ticket.id)
    assert saved.status == TicketStatus.RESOLVED
    assert saved.resolved_at is not None
    assert "checked, safe to close" in saved.resolution_notes


@pytest.mark.asyncio
async def test_approve_to_resolve_needs_notes(paused_ticket, queued, monkeypatch):
    ticket, token = paused_ticket
    graph, calls = fake_graph({})
    monkeypatch.setattr(agents_api, "get_supervisor_graph", graph)
    monkeypatch.setattr(supervisor, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id, next_agent="END", notes="   ")

    assert resp.status_code == 422
    assert "resume" not in calls
    assert (await _load(ticket.id)).status == TicketStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_reject_is_recorded_without_the_worker(paused_ticket, queued, monkeypatch):
    ticket, token = paused_ticket
    graph, _ = fake_graph({"final_resolution": "[HUMAN REVIEW] not safe\n\n", "hitl_required": False, "next": "END"})
    monkeypatch.setattr(agents_api, "get_supervisor_graph", graph)
    monkeypatch.setattr(supervisor, "get_supervisor_graph", graph)

    resp = await _resume(token, ticket.id, approved=False, notes="not safe")

    assert resp.json()["status"] == "rejected"
    assert queued == []
    saved = await _load(ticket.id)
    assert saved.status == TicketStatus.IN_PROGRESS
    assert saved.assigned_agent == AssignedAgent.HUMAN
    assert "not safe" in saved.resolution_notes


# ── The worker side: resume the graph and save the outcome ──


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

    await supervisor.resume_supervisor(str(ticket.id), _review(notes="ok to restart"))

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

    await supervisor.resume_supervisor(str(ticket.id), _review(approved=False, notes="escalate to DBA team"))

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

    await supervisor.resume_supervisor(str(ticket.id), _review())

    saved = await _load(ticket.id)
    assert saved.status == TicketStatus.IN_PROGRESS
    assert saved.assigned_agent == AssignedAgent.HUMAN
    assert saved.diagnosis_output["root_cause"] == "max_connections reached"


@pytest.mark.asyncio
async def test_resuming_a_run_that_is_not_paused_raises(paused_ticket, monkeypatch):
    ticket, _ = paused_ticket
    graph, calls = fake_graph({}, paused=False)
    monkeypatch.setattr(supervisor, "get_supervisor_graph", graph)

    with pytest.raises(supervisor.NoPausedRunError):
        await supervisor.resume_supervisor(str(ticket.id), _review())

    assert "resume" not in calls          # graph was not invoked


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
