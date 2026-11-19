"""
NEXUS AI · tests/test_agent_runs.py
Agent runs that lose their connection or their worker: automatic retries,
the stall check, and the Retry agents endpoint.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.security import create_access_token, hash_password
from app.database import Base, get_async_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.ticket import AssignedAgent, Ticket, TicketStatus
from app.models.user import User, UserRole
from app.workers import agent_runs
from app.workers.celery_app import process_ticket_task

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
async def ticket():
    """An in-progress ticket the Triage agent has, plus an agent's token."""
    async with TestSessionLocal() as db:
        user = User(email="runs@nexus.ai", username="runs_agent", full_name="Runs Agent",
                    role=UserRole.AGENT, hashed_password=hash_password("testpass123"))
        db.add(user)
        await db.flush()
        t = Ticket(ticket_number=f"NX-2026-{uuid.uuid4().int % 100000:05d}", title="Oracle alerts not arriving",
                   description="Alert e-mails from OEM stopped at 10:00.", created_by_id=user.id,
                   status=TicketStatus.IN_PROGRESS, assigned_agent=AssignedAgent.TRIAGE)
        db.add(t)
        await db.commit()
        token = create_access_token(subject=str(user.id), extra_claims={"role": "agent", "email": user.email})
    tid = str(t.id)
    from app.database import engine
    await engine.dispose()          # the app's pool must not carry connections across test loops
    agent_runs.clear_beat(tid)
    agent_runs.release_lock(tid)
    yield tid, token
    await engine.dispose()
    agent_runs.clear_beat(tid)
    agent_runs.release_lock(tid)


async def _load(tid):
    async with TestSessionLocal() as db:
        return await db.get(Ticket, uuid.UUID(tid))


def load(tid):
    """For the synchronous tests (the Celery task runs its own event loop)."""
    return asyncio.run(_load(tid))


def _connection_error():
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))


class RetryCalled(Exception):
    pass


@pytest.fixture
def run_task(monkeypatch):
    """Run process_ticket_task in-process with a fake supervisor; returns what happened."""
    import agents.supervisor as supervisor
    from app.workers import integration_tasks

    seen = {"supervisor_calls": 0, "retry": None}

    def run(tid, outcome, retries=0):
        async def fake_run_supervisor(ticket_id, callbacks=None):
            seen["supervisor_calls"] += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def fake_retry(exc=None, countdown=None, **_):
            seen["retry"] = countdown
            raise RetryCalled()

        monkeypatch.setattr(supervisor, "run_supervisor", fake_run_supervisor)
        monkeypatch.setattr(process_ticket_task, "retry", fake_retry)
        monkeypatch.setattr(integration_tasks, "queue_write_back", lambda ticket_id: None)
        process_ticket_task.push_request(retries=retries)
        try:
            process_ticket_task.run(tid)
        except RetryCalled:
            pass
        finally:
            process_ticket_task.pop_request()
        return seen

    return run


# ── Classifying errors ─────────────────────────────────────────
def test_network_errors_are_transient():
    import socket

    assert agent_runs.is_transient(_connection_error())
    assert agent_runs.is_transient(socket.gaierror(-3, "Temporary failure in name resolution"))
    assert agent_runs.is_transient(httpx.ConnectTimeout("timed out"))
    try:
        try:
            raise _connection_error()
        except Exception as inner:
            raise RuntimeError("graph step failed") from inner
    except RuntimeError as wrapped:
        assert agent_runs.is_transient(wrapped)
    assert not agent_runs.is_transient(ValueError("bad JSON from the model"))


# ── The Celery task ────────────────────────────────────────────
def test_lost_connection_schedules_a_retry(ticket, run_task):
    tid, _ = ticket
    seen = run_task(tid, _connection_error())

    assert seen["retry"] == 60
    t = load(tid)
    assert t.assigned_agent == AssignedAgent.TRIAGE          # still with the agents
    assert "couldn't reach OpenAI" in t.agent_error and "attempt 2 of 4" in t.agent_error
    assert agent_runs.is_alive(tid)                          # the stall check leaves it alone
    assert agent_runs.acquire_lock(tid)                      # the lock was released for the retry


def test_retries_back_off(ticket, run_task):
    tid, _ = ticket
    assert run_task(tid, _connection_error(), retries=1)["retry"] == 120
    agent_runs.release_lock(tid)
    assert run_task(tid, _connection_error(), retries=2)["retry"] == 240


def test_last_retry_failing_hands_the_ticket_to_a_human(ticket, run_task):
    tid, _ = ticket
    seen = run_task(tid, _connection_error(), retries=3)

    assert seen["retry"] is None
    t = load(tid)
    assert t.status == TicketStatus.IN_PROGRESS and t.assigned_agent == AssignedAgent.HUMAN
    assert "couldn't reach OpenAI" in t.agent_error and "Retry agents" in t.agent_error
    assert not agent_runs.is_alive(tid)


def test_other_errors_stop_at_once(ticket, run_task):
    tid, _ = ticket
    seen = run_task(tid, ValueError("model returned no JSON"))

    assert seen["retry"] is None
    t = load(tid)
    assert t.assigned_agent == AssignedAgent.HUMAN
    assert t.agent_error.startswith("ValueError: model returned no JSON")
    async def actions():
        async with TestSessionLocal() as db:
            return (await db.execute(select(AuditLog.action))).scalars().all()
    assert "agent.run_stopped" in asyncio.run(actions())


def test_a_second_run_of_the_same_ticket_is_skipped(ticket, run_task):
    tid, _ = ticket
    assert agent_runs.acquire_lock(tid)                      # a run is going
    seen = run_task(tid, {"final_resolution": "x"})
    assert seen["supervisor_calls"] == 0


def test_late_job_for_a_ticket_given_to_a_human_is_skipped(ticket, run_task):
    """E.g. a job redelivered an hour after a worker died, when the ticket has moved on."""
    tid, _ = ticket

    async def to_human():
        async with TestSessionLocal() as db:
            await db.execute(update(Ticket).where(Ticket.id == uuid.UUID(tid))
                             .values(assigned_agent=AssignedAgent.HUMAN))
            await db.commit()
    asyncio.run(to_human())
    seen = run_task(tid, {"final_resolution": "x"})
    assert seen["supervisor_calls"] == 0


def test_successful_run_clears_heartbeat_and_lock(ticket, run_task):
    tid, _ = ticket
    seen = run_task(tid, {"final_resolution": "Restarted the OEM agent", "error": None})
    assert seen["supervisor_calls"] == 1
    assert not agent_runs.is_alive(tid)


# ── The stall check ────────────────────────────────────────────
async def _age(tid, minutes):
    async with TestSessionLocal() as db:
        await db.execute(update(Ticket).where(Ticket.id == uuid.UUID(tid)).values(
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes)))
        await db.commit()


@pytest.mark.asyncio
async def test_stalled_run_is_handed_to_a_human(ticket):
    tid, _ = ticket
    await _age(tid, 10)
    stopped = await agent_runs.recover_stalled_runs()
    t = await _load(tid)
    assert stopped == [t.ticket_number]
    assert t.assigned_agent == AssignedAgent.HUMAN
    assert "stopped without finishing" in t.agent_error


@pytest.mark.asyncio
async def test_live_or_recent_runs_are_left_alone(ticket):
    tid, _ = ticket
    await _age(tid, 2)                                       # started 2 minutes ago
    assert await agent_runs.recover_stalled_runs() == []
    await _age(tid, 10)
    agent_runs.beat(tid)                                     # old, but still beating
    assert await agent_runs.recover_stalled_runs() == []
    assert (await _load(tid)).assigned_agent == AssignedAgent.TRIAGE


# ── Retry agents ───────────────────────────────────────────────
def _no_paused_graph(monkeypatch):
    from app.api.v1 import agents as agents_api

    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(next=(), values={})

    @asynccontextmanager
    async def _get():
        yield Graph()

    monkeypatch.setattr(agents_api, "get_supervisor_graph", _get)


async def _retry(tid, token):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(f"/api/v1/agents/retry/{tid}", headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_retry_agents_restarts_a_stopped_run(ticket, monkeypatch):
    tid, token = ticket
    await agent_runs.stop_run(tid, "NEXUS couldn't reach OpenAI.")
    _no_paused_graph(monkeypatch)
    queued = []
    monkeypatch.setattr(process_ticket_task, "delay", lambda ticket_id: queued.append(ticket_id) or SimpleNamespace(id="t1"))

    resp = await _retry(tid, token)

    assert resp.status_code == 200, resp.text
    assert queued == [tid]
    t = await _load(tid)
    assert t.assigned_agent == AssignedAgent.TRIAGE and t.agent_error is None
    assert agent_runs.is_alive(tid)                          # queued counts as alive


@pytest.mark.asyncio
async def test_retry_refused_while_a_run_is_going(ticket, monkeypatch):
    tid, token = ticket
    agent_runs.beat(tid)
    _no_paused_graph(monkeypatch)
    resp = await _retry(tid, token)
    assert resp.status_code == 409 and "still working" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_retry_refused_while_waiting_for_review(ticket, monkeypatch):
    from app.api.v1 import agents as agents_api

    tid, token = ticket
    await agent_runs.stop_run(tid, "paused")

    class Graph:
        async def aget_state(self, config):
            return SimpleNamespace(next=("human_review",), values={})

    @asynccontextmanager
    async def _get():
        yield Graph()

    monkeypatch.setattr(agents_api, "get_supervisor_graph", _get)
    resp = await _retry(tid, token)
    assert resp.status_code == 409 and "review" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ticket_response_includes_the_reason(ticket):
    tid, token = ticket
    await agent_runs.stop_run(tid, "NEXUS couldn't reach OpenAI.")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        body = (await c.get(f"/api/v1/tickets/{tid}", headers={"Authorization": f"Bearer {token}"})).json()
    assert body["agent_error"].startswith("NEXUS couldn't reach OpenAI.")
