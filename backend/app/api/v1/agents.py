"""
NEXUS AI · app/api/v1/agents.py
Agent orchestration endpoints:
  POST /agents/run/{ticket_id}     — trigger supervisor for a ticket
  GET  /agents/status/{ticket_id}  — get current graph state
  POST /agents/resume/{ticket_id}  — resume after HITL checkpoint
  GET  /agents/trace/{ticket_id}   — return message trace for UI
"""

from logging import config
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from langgraph import graph
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.supervisor import get_supervisor_graph
from app.core.deps import get_current_user, require_agent_or_admin
from app.database import get_async_db
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User

router = APIRouter(prefix="/agents", tags=["Agents"])


class HITLResumeRequest(BaseModel):
    approved: bool
    next_agent: str = "diagnosis_agent"
    notes: str = ""


class AgentRunResponse(BaseModel):
    ticket_id: str
    ticket_number: str
    status: str
    message: str
    task_id: str | None = None


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

    # Dispatch via Celery for production async execution
    from app.workers.celery_app import process_ticket_task
    task = process_ticket_task.delay(str(ticket_id))

    return AgentRunResponse(
        ticket_id=str(ticket_id),
        ticket_number=ticket.ticket_number,
        status="dispatched",
        message="Agent supervisor started. Poll /agents/status for updates.",
        task_id=task.id,
    )


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
    Resume a paused graph after human review (HITL checkpoint).
    Supply approved=true and optionally override the next_agent.
    """
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    try:
        from agents.supervisor import get_supervisor_graph
        from langgraph.types import Command

        config = {"configurable": {"thread_id": str(ticket_id)}}
        human_response = {
            "approved": body.approved,
            "next_agent": body.next_agent,
            "notes": body.notes,
        }
        async with get_supervisor_graph() as graph:
            final_state = await graph.ainvoke(Command(resume=human_response), config=config)
        return {"status": "resumed", "final_state_keys": list(final_state.keys())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resume failed: {str(e)}")


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
        }
    except Exception as e:
        return {"trace": [], "error": str(e)}
