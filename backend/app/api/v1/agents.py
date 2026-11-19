"""
NEXUS AI · app/api/v1/agents.py
Agent orchestration endpoints:
  POST /agents/run/{ticket_id}     — trigger supervisor for a ticket
  GET  /agents/status/{ticket_id}  — get current graph state
  POST /agents/resume/{ticket_id}  — submit a human review: resolve, reject, or send back to an agent
  GET  /agents/trace/{ticket_id}   — return message trace for UI
  GET  /agents/info                — which models the agents use (from config)
  GET  /agents/active              — in-progress tickets with each run's live step
"""

from logging import config
import uuid
from typing import Literal
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from langgraph import graph
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.supervisor import get_supervisor_graph
from app.core.deps import get_current_user, require_agent_or_admin
from app.database import get_async_db
from app.models.ticket import AssignedAgent, Ticket, TicketStatus
from app.models.user import User, UserRole

router = APIRouter(prefix="/agents", tags=["Agents"])


# Graph node name → label shown in the UI for a run that is about to run / running it
NODE_STEP_LABELS = {
    "__start__": "Starting",
    "triage_agent": "Triage",
    "security_agent": "Security",
    "diagnosis_agent": "Diagnosis",
    "knowledge_agent": "Knowledge",
    "human_review": "Awaiting human review",
    "guard": "Stopping (iteration limit)",
}


def live_step(next_nodes: tuple | None, has_checkpoint: bool, assigned_agent: AssignedAgent) -> str:
    """
    Describe where a ticket's agent run is right now.

    LangGraph saves a checkpoint after every step; its `next` names the node that
    runs next (i.e. the one executing now). An empty `next` means the graph has
    stopped, so we fall back to whether a human has taken the ticket over.
    """
    if next_nodes:
        return NODE_STEP_LABELS.get(next_nodes[0], next_nodes[0])
    if assigned_agent == AssignedAgent.HUMAN:
        return "With a human"
    return "Finishing" if has_checkpoint else "Queued"


class HITLResumeRequest(BaseModel):
    approved: bool
    # What happens when approved:
    #   "END"      — the reviewer's notes resolve the ticket; no further agents run
    #   an agent   — the run continues with that agent on the worker
    # Restricted to real graph nodes: an unknown name would crash the run at the routing step.
    next_agent: Literal["END", "diagnosis_agent", "security_agent", "knowledge_agent"] = "diagnosis_agent"
    notes: str = ""


class AgentRunResponse(BaseModel):
    ticket_id: str
    ticket_number: str
    status: str
    message: str
    task_id: str | None = None


# A queued run counts as alive for this long before the worker picks it up
QUEUED_HEARTBEAT_TTL = 1200


@router.post("/run/{ticket_id}", response_model=AgentRunResponse)
async def trigger_agent_run(
    ticket_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_agent_or_admin),
):
    """
    Trigger the LangGraph supervisor for a ticket.
    Runs as a background task — returns immediately with task reference.
    Poll /agents/status/{ticket_id} for progress.
    """
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    if ticket.status == TicketStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Agent run already in progress for this ticket")

    # Mark the run as started now, not when the worker picks it up: the UI sees
    # it immediately and a second click hits the 409 above instead of starting
    # a duplicate run.
    previous = (ticket.status, ticket.assigned_agent)
    ticket.status = TicketStatus.IN_PROGRESS
    ticket.assigned_agent = AssignedAgent.TRIAGE
    ticket.agent_error = None
    await db.commit()

    # Dispatch via Celery for production async execution
    from app.workers import agent_runs
    from app.workers.celery_app import process_ticket_task
    agent_runs.beat(str(ticket_id), ttl=QUEUED_HEARTBEAT_TTL)   # queued counts as alive
    try:
        task = process_ticket_task.delay(str(ticket_id))
    except Exception as e:
        # Nothing was queued (broker unreachable): put the ticket back
        agent_runs.clear_beat(str(ticket_id))
        ticket.status, ticket.assigned_agent = previous
        await db.commit()
        raise HTTPException(status_code=503, detail=f"Could not queue the agent run: {e}")

    return AgentRunResponse(
        ticket_id=str(ticket_id),
        ticket_number=ticket.ticket_number,
        status="dispatched",
        message="Agent supervisor started. Poll /agents/status for updates.",
        task_id=task.id,
    )


@router.get("/info")
async def get_agent_info(current_user: User = Depends(get_current_user)):
    """Models the agents actually use, read from config so the UI never hard-codes them."""
    from rag.config import rag_settings

    embedding_model = (
        rag_settings.embedding_model_openai
        if rag_settings.embedding_provider.value == "openai"
        else rag_settings.embedding_model_ollama
    )
    return {
        "llm_model": rag_settings.llm_model,
        "embedding_provider": rag_settings.embedding_provider.value,
        "embedding_model": embedding_model,
        "reranker_model": rag_settings.reranker_model,
    }


@router.get("/active")
async def get_active_runs(
    limit: int = 10,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    """
    In-progress tickets plus the live step of each agent run, read from the
    LangGraph checkpoint (not from assigned_agent, which is only set at the start).
    """
    query = select(Ticket).where(Ticket.status == TicketStatus.IN_PROGRESS)
    if current_user.role == UserRole.USER:
        query = query.where(Ticket.created_by_id == current_user.id)
    query = query.order_by(Ticket.updated_at.desc()).limit(max(1, min(limit, 50)))
    tickets = (await db.execute(query)).scalars().all()

    snapshots: dict[str, object] = {}
    if tickets:
        try:
            async with get_supervisor_graph() as graph:
                for t in tickets:
                    snapshots[str(t.id)] = await graph.aget_state(
                        {"configurable": {"thread_id": str(t.id)}}
                    )
        except Exception:
            snapshots = {}   # Redis unreachable: still list the runs, just without a live step

    items = []
    for t in tickets:
        snap = snapshots.get(str(t.id))
        has_checkpoint = bool(snap and snap.values)
        next_nodes = tuple(snap.next) if snap and snap.next else None
        items.append({
            "id": str(t.id),
            "ticket_number": t.ticket_number,
            "title": t.title,
            "priority": t.priority.value,
            "assigned_agent": t.assigned_agent.value,
            "current_step": live_step(next_nodes, has_checkpoint, t.assigned_agent)
                            if snapshots else "Unknown",
            "current_node": next_nodes[0] if next_nodes else None,
            "iteration": snap.values.get("iteration", 0) if has_checkpoint else 0,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        })
    return {"items": items, "total": len(items)}


@router.get("/status/{ticket_id}")
async def get_agent_status(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get the current processing status of a ticket through the agent pipeline.
    Returns ticket status + agent outputs completed so far.
    """
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    return {
        "ticket_id": str(ticket_id),
        "ticket_number": ticket.ticket_number,
        "status": ticket.status.value,
        "assigned_agent": ticket.assigned_agent.value,
        "triage_complete": bool(ticket.triage_output),
        "diagnosis_complete": bool(ticket.diagnosis_output),
        "resolution_available": bool(ticket.resolution_notes),
        "sla_breached": ticket.sla_breached,
        "langsmith_run_id": ticket.langsmith_run_id,
        "triage_output": ticket.triage_output,
        "diagnosis_output": ticket.diagnosis_output,
    }


@router.post("/resume/{ticket_id}")
async def resume_after_hitl(
    ticket_id: uuid.UUID,
    body: HITLResumeRequest,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_agent_or_admin),
):
    """
    Submit the human review for a run paused at the HITL checkpoint.

      approved=true,  next_agent="END"   → ticket resolved with the review notes
      approved=true,  next_agent=<agent> → run continues with that agent
      approved=false                     → agents stop, ticket stays with a human

    Resolving and rejecting involve no agent work, so they finish before this
    returns. Continuing with an agent can take minutes, so that is queued for
    the Celery worker; follow it with /agents/trace or /agents/active.
    """
    resolving = body.approved and body.next_agent == "END"
    if resolving and not body.notes.strip():
        raise HTTPException(
            status_code=422,
            detail="Review notes are required to resolve the ticket: they become its resolution.",
        )

    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    try:
        async with get_supervisor_graph() as graph:
            snapshot = await graph.aget_state({"configurable": {"thread_id": str(ticket_id)}})
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not read the agent run: {e}")

    # A paused run always leaves the ticket with a human. Once a review is
    # submitted the ticket goes back to the agents (below), so a second
    # submission for the same pause is refused here.
    waiting = bool(snapshot and snapshot.next and snapshot.next[0] == "human_review")
    if not waiting or ticket.assigned_agent != AssignedAgent.HUMAN:
        raise HTTPException(status_code=409, detail="No agent run is waiting for human review on this ticket")

    human_response = {
        "approved": body.approved,
        "next_agent": body.next_agent,
        "notes": body.notes.strip(),
    }

    if resolving or not body.approved:
        # No agent runs: finish the graph here and save the outcome to the ticket
        from agents.supervisor import NoPausedRunError, resume_supervisor
        await db.commit()       # release this session's hold on the ticket before the outcome is written
        try:
            await resume_supervisor(str(ticket_id), human_response)
        except NoPausedRunError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not record the review: {e}")
        return {"status": "resolved" if resolving else "rejected", "approved": body.approved}

    ticket.assigned_agent = AssignedAgent(body.next_agent)
    await db.commit()

    from app.workers import agent_runs
    from app.workers.celery_app import resume_ticket_task
    agent_runs.beat(str(ticket_id), ttl=QUEUED_HEARTBEAT_TTL)   # queued counts as alive
    try:
        task = resume_ticket_task.delay(str(ticket_id), human_response)
    except Exception as e:
        # Nothing was queued (broker unreachable): the ticket is still waiting for review
        agent_runs.clear_beat(str(ticket_id))
        ticket.assigned_agent = AssignedAgent.HUMAN
        await db.commit()
        raise HTTPException(status_code=503, detail=f"Could not queue the review: {e}")

    return {"status": "dispatched", "approved": body.approved, "task_id": task.id}


@router.get("/trace/{ticket_id}")
async def get_agent_trace(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return the message trace from the agent pipeline for display in the UI.
    Shows which agents ran, what they decided, and in what order.
    """
    try:
        from agents.supervisor import get_supervisor_graph
        config = {"configurable": {"thread_id": str(ticket_id)}}
        async with get_supervisor_graph() as graph:
            state = await graph.aget_state(config)
        if not state or not state.values:
            return {"trace": [], "message": "No agent trace available yet"}

        messages = state.values.get("messages", [])
        trace = [
            {
                "role": msg.type if hasattr(msg, "type") else "unknown",
                "content": msg.content,
            }
            for msg in messages
        ]
        return {
            "ticket_id": str(ticket_id),
            "trace": trace,
            "current_node": state.next,
            "iteration": state.values.get("iteration", 0),
            "hitl_required": state.values.get("hitl_required", False),
            "hitl_reason": state.values.get("hitl_reason") or "",
        }
    except Exception as e:
        return {"trace": [], "error": str(e)}


# ── LangSmith trace link ──────────────────────────────────────
_langsmith_project_cache: dict[str, tuple[str, str]] = {}   # project name → (host url, project id)


def _langsmith_base(project_name: str) -> str:
    """
    URL prefix for runs in this LangSmith project, e.g.
    https://smith.langchain.com/o/<org>/projects/p/<project>. Looked up once per process.
    """
    if project_name not in _langsmith_project_cache:
        from types import SimpleNamespace

        from langsmith import Client

        from app.config import settings

        client = Client(api_key=settings.langsmith_api_key)
        project_id = client.read_project(project_name=project_name).id
        probe = client.get_run_url(run=SimpleNamespace(id="RUN"), project_id=project_id)
        _langsmith_project_cache[project_name] = (probe.split("/r/RUN")[0], str(project_id))
    return _langsmith_project_cache[project_name][0]


@router.get("/langsmith/{ticket_id}")
async def get_langsmith_link(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_agent_or_admin),
):
    """
    Link to the LangSmith trace of the ticket's latest agent run.
    {"run_id": str | None, "url": str | None, "reason": str (when url is None)}
    """
    import asyncio

    from app.config import settings

    ticket = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    run_id = ticket.langsmith_run_id
    if not run_id:
        return {"run_id": None, "url": None, "reason": "No traced agent run for this ticket yet"}
    if not settings.langsmith_api_key:
        return {"run_id": run_id, "url": None, "reason": "LangSmith is not configured"}
    try:
        base = await asyncio.to_thread(_langsmith_base, settings.langsmith_project)
    except Exception as e:
        return {"run_id": run_id, "url": None, "reason": f"Could not reach LangSmith: {e}"}
    return {"run_id": run_id, "url": f"{base}/r/{run_id}?poll=true"}


# ── Retry a run that stopped ──────────────────────────────────
@router.post("/retry/{ticket_id}", response_model=AgentRunResponse)
async def retry_agent_run(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_agent_or_admin),
):
    """
    Start the agents again, from Triage, on a ticket whose run stopped (lost
    connection, worker restart, an error) or that a human was given.
    409 if a run is still going or the ticket is waiting for a human review.
    """
    from app.models.audit_log import AuditLog
    from app.workers import agent_runs
    from app.workers.celery_app import process_ticket_task

    ticket = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket.status != TicketStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Only an in-progress ticket can be retried; use Run Agents")
    if agent_runs.is_alive(str(ticket_id)):
        raise HTTPException(status_code=409, detail="The agents are still working on this ticket, "
                                                    "or a retry is already scheduled")
    try:
        async with get_supervisor_graph() as graph:
            snap = await graph.aget_state({"configurable": {"thread_id": str(ticket_id)}})
        if snap and snap.next and "human_review" in snap.next:
            raise HTTPException(status_code=409, detail="This ticket is waiting for a human review; "
                                                        "submit the review instead")
    except HTTPException:
        raise
    except Exception:
        pass    # can't read the checkpoint: a fresh run is still fine

    previous = (ticket.assigned_agent, ticket.agent_error)
    ticket.assigned_agent = AssignedAgent.TRIAGE
    ticket.agent_error = None
    db.add(AuditLog(user_id=current_user.id, action="agent.retry", resource_type="ticket",
                    resource_id=str(ticket_id), details={"previous_error": previous[1]}))
    await db.commit()

    agent_runs.beat(str(ticket_id), ttl=QUEUED_HEARTBEAT_TTL)
    try:
        task = process_ticket_task.delay(str(ticket_id))
    except Exception as e:
        agent_runs.clear_beat(str(ticket_id))
        ticket.assigned_agent, ticket.agent_error = previous
        await db.commit()
        raise HTTPException(status_code=503, detail=f"Could not queue the agent run: {e}")

    return AgentRunResponse(
        ticket_id=str(ticket_id),
        ticket_number=ticket.ticket_number,
        status="dispatched",
        message="Agents restarted from Triage.",
        task_id=task.id,
    )
