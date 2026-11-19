"""
NEXUS AI · app/workers/agent_runs.py
Keeps agent runs from getting stuck when the power or the internet drops.

  - Heartbeat: a running agent refreshes a Redis key at every step. A ticket that
    is "in progress" with an agent but has no heartbeat isn't being worked on.
  - Lock: one agent run per ticket at a time, whatever starts it (a click, a
    retry, a redelivered job after a worker restart).
  - Transient errors: a lost connection to OpenAI (or DNS, or a timeout) is
    retried by the Celery task; other errors stop the run straight away.
  - stop_run(): hands the ticket to a human with the reason saved on it.
  - recover_stalled_runs(): the periodic check that finds runs that died
    without a word (worker killed by a power cut, laptop asleep...).
"""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

HEARTBEAT_TTL = 300          # seconds; refreshed at every graph step and LLM call
LOCK_TTL = 1800              # an agent run never takes this long; the lock frees itself
STALL_AFTER = timedelta(minutes=5)
RETRY_DELAYS = (60, 120, 240)    # seconds before retry 1, 2, 3

_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        import redis
        from app.config import settings
        _redis_client = redis.Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
    return _redis_client


def _hb_key(ticket_id) -> str:
    return f"nexus:agent_heartbeat:{ticket_id}"


def _lock_key(ticket_id) -> str:
    return f"nexus:agent_lock:{ticket_id}"


# ── Heartbeat ──────────────────────────────────────────────────
def beat(ticket_id, ttl: int = HEARTBEAT_TTL) -> None:
    """Mark the ticket's agent run as alive for `ttl` seconds."""
    try:
        _redis().set(_hb_key(ticket_id), str(int(time.time())), ex=int(ttl))
    except Exception as e:      # Redis down: the run itself will fail and be handled
        logger.debug(f"heartbeat not written for {ticket_id}: {e}")


def clear_beat(ticket_id) -> None:
    try:
        _redis().delete(_hb_key(ticket_id))
    except Exception:
        pass


def is_alive(ticket_id) -> bool:
    """True if a run (or a scheduled retry) is looking after this ticket."""
    try:
        return bool(_redis().exists(_hb_key(ticket_id))) or bool(_redis().exists(_lock_key(ticket_id)))
    except Exception:
        return True     # can't tell: never interrupt a run we can't see


# ── Lock ───────────────────────────────────────────────────────
def acquire_lock(ticket_id) -> bool:
    try:
        return bool(_redis().set(_lock_key(ticket_id), "1", nx=True, ex=LOCK_TTL))
    except Exception:
        return True     # Redis down: Celery can't deliver tasks either, so no duplicate


def release_lock(ticket_id) -> None:
    try:
        _redis().delete(_lock_key(ticket_id))
    except Exception:
        pass


def heartbeat_callback(ticket_id: str):
    """
    LangChain callback that refreshes the heartbeat whenever a graph step,
    LLM call or tool starts (at most every 15 s). Pass it in the run's callbacks.
    """
    from langchain_core.callbacks import BaseCallbackHandler

    class HeartbeatCallback(BaseCallbackHandler):
        raise_error = False

        def __init__(self):
            self._last = 0.0

        def _tick(self, *_, **__):
            now = time.monotonic()
            if now - self._last >= 15:
                self._last = now
                beat(ticket_id)

        on_chain_start = on_llm_start = on_chat_model_start = on_tool_start = _tick

    return HeartbeatCallback()


# ── Errors ─────────────────────────────────────────────────────
def _transient_types() -> tuple[type, ...]:
    types: list[type] = [ConnectionError, TimeoutError, asyncio.TimeoutError, socket.gaierror]
    try:
        import openai
        types += [openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError]
    except ImportError:
        pass
    try:
        import httpx
        types.append(httpx.TransportError)
    except ImportError:
        pass
    return tuple(types)


def is_transient(exc: BaseException) -> bool:
    """A network-type failure worth retrying (checks the whole cause chain)."""
    types = _transient_types()
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, types):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def describe(exc: BaseException) -> str:
    """One line a person can understand, for the ticket page."""
    if is_transient(exc):
        return "NEXUS couldn't reach OpenAI (lost internet connection or a timeout)."
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text[:300]}" if text else type(exc).__name__


# ── Ticket updates ─────────────────────────────────────────────
async def note_retry(ticket_id: str, reason: str, attempt: int, total: int, delay: int) -> None:
    """Keep the ticket with the agents and say when the next attempt is."""
    from sqlalchemy import select

    from app.database import get_db_context
    from app.models.audit_log import AuditLog
    from app.models.ticket import Ticket

    at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%H:%M UTC")
    message = f"{reason} Retrying automatically at {at} (attempt {attempt} of {total})."
    async with get_db_context() as db:
        t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
        if t:
            t.agent_error = message
            db.add(AuditLog(user_id=None, action="agent.run_retry_scheduled", resource_type="ticket",
                            resource_id=str(ticket_id), details={"reason": reason, "attempt": attempt,
                                                                 "delay_seconds": delay}))


async def stop_run(ticket_id: str, reason: str, action: str = "agent.run_stopped") -> bool:
    """
    The agents have stopped for good: give the ticket to a human with the reason.
    Returns False if the ticket no longer needs it (finished meanwhile, or gone).
    """
    from sqlalchemy import select

    from app.database import get_db_context
    from app.models.audit_log import AuditLog
    from app.models.ticket import AssignedAgent, Ticket, TicketStatus

    async with get_db_context() as db:
        t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
        if not t or t.status != TicketStatus.IN_PROGRESS:
            return False
        t.assigned_agent = AssignedAgent.HUMAN
        t.agent_error = f"{reason} The agents have stopped; use Retry agents to run them again."
        db.add(AuditLog(user_id=None, action=action, resource_type="ticket",
                        resource_id=str(ticket_id), details={"reason": reason}))
    clear_beat(ticket_id)
    logger.warning(f"Agent run for ticket {ticket_id} stopped: {reason}")
    return True


async def run_still_wanted(ticket_id: str) -> bool:
    """
    False for a job that arrives after the ticket moved on: resolved/closed,
    or already handed to a human (e.g. a job redelivered after a worker restart).
    """
    from sqlalchemy import select

    from app.database import get_db_context
    from app.models.ticket import AssignedAgent, Ticket, TicketStatus

    async with get_db_context() as db:
        t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
        if not t:
            return False
        if t.status not in (TicketStatus.OPEN, TicketStatus.TRIAGED, TicketStatus.IN_PROGRESS):
            return False
        return t.assigned_agent != AssignedAgent.HUMAN


async def clear_error(ticket_id: str) -> None:
    from sqlalchemy import select

    from app.database import get_db_context
    from app.models.ticket import Ticket

    async with get_db_context() as db:
        t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
        if t and t.agent_error:
            t.agent_error = None


# ── Periodic check ─────────────────────────────────────────────
STALLED_REASON = ("The agent run stopped without finishing, most likely because the worker "
                  "restarted or the computer lost power or its connection.")


async def recover_stalled_runs() -> list[str]:
    """
    Tickets "in progress" with an agent, untouched for STALL_AFTER and with no
    heartbeat, lock or scheduled retry: hand them to a human. Returns their numbers.
    """
    from sqlalchemy import select

    from app.database import get_db_context
    from app.models.ticket import AssignedAgent, Ticket, TicketStatus

    cutoff = datetime.now(timezone.utc) - STALL_AFTER
    async with get_db_context() as db:
        rows = (await db.execute(
            select(Ticket.id, Ticket.ticket_number).where(
                Ticket.status == TicketStatus.IN_PROGRESS,
                Ticket.assigned_agent != AssignedAgent.HUMAN,
                Ticket.updated_at < cutoff,
            )
        )).all()

    stopped = []
    for ticket_id, number in rows:
        if is_alive(ticket_id):
            continue
        if await _paused_for_review(ticket_id):
            continue
        if await stop_run(str(ticket_id), STALLED_REASON, action="agent.run_stalled"):
            stopped.append(number)
    return stopped


async def _paused_for_review(ticket_id) -> bool:
    """A run waiting at the human-review step is paused on purpose, not stuck."""
    try:
        from agents.supervisor import get_supervisor_graph
        async with get_supervisor_graph() as graph:
            snap = await graph.aget_state({"configurable": {"thread_id": str(ticket_id)}})
        return bool(snap and snap.next and "human_review" in snap.next)
    except Exception:
        return False
