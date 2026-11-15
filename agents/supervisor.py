"""
NEXUS AI · agents/supervisor.py
LangGraph Supervisor — the orchestration brain.

Graph topology:
  START → triage_agent
        → security_agent (if security ticket)  → diagnosis_agent
        → diagnosis_agent                       → knowledge_agent
        → human_review (HITL checkpoint)        → diagnosis_agent or END
        → knowledge_agent                       → END

Features:
  - Conditional routing based on state["next"]
  - HITL interrupt at human_review node
  - Max iteration guard (prevents loops)
  - Full state persistence via MemorySaver (in-memory for dev)
"""

from langgraph import graph
from langgraph.graph import StateGraph, END
#from langgraph.checkpoint.memory import MemorySaver  # I changed it to use RedisSaver for production
from langgraph.checkpoint.redis import AsyncRedisSaver
from app.config import settings
from agents.state import TicketState
from agents.triage_agent import triage_agent
from agents.diagnosis_agent import diagnosis_agent
from agents.knowledge_agent import knowledge_agent
from agents.security_agent import security_agent
from contextlib import asynccontextmanager
#from backend.app import config


MAX_ITERATIONS = 8


# ── Human Review node (HITL) ──────────────────────────────────
async def human_review_node(state: TicketState) -> TicketState:
    """
    HITL checkpoint — the graph PAUSES here and waits for human input.
    LangGraph's interrupt() mechanism handles the actual pause.
    When the human approves, execution resumes from this node.
    The human can update state["next"] to redirect flow.
    """
    from langgraph.types import interrupt

    # Pause and surface the state to the human operator
    human_decision = interrupt({
        "message": "Human review required before proceeding.",
        "reason": state.get("hitl_reason", "Escalation triggered"),
        "ticket": state["ticket_number"],
        "triage": state.get("triage_result", {}),
        "security": state.get("security_result", {}),
        "instruction": "Set 'approved' to true and optionally override 'next_agent'.",
    })

    # Human response should contain {"approved": bool, "next_agent": str, "notes": str}
    if isinstance(human_decision, dict):
        approved = human_decision.get("approved", False)
        notes = human_decision.get("notes", "")
        next_agent = human_decision.get("next_agent", "diagnosis_agent")
    else:
        approved = False
        notes = ""
        next_agent = "END"

    return {
        **state,
        "next": next_agent if approved else "END",
        "hitl_required": False,
        "final_resolution": (
            f"[HUMAN REVIEW] {notes}\n\n" + state.get("final_resolution", "")
        ) if notes else state.get("final_resolution", ""),
    }


# ── Guard node — prevents infinite loops ─────────────────────
async def guard_node(state: TicketState) -> TicketState:
    """Safety guard: force END if iteration limit reached."""
    return {
        **state,
        "next": "END",
        "error": f"Max iterations ({MAX_ITERATIONS}) reached. Manual review required.",
    }


# ── Routing function ─────────────────────────────────────────
def route(state: TicketState) -> str:
    """Read state["next"] and return the next node name."""
    if state.get("iteration", 0) >= MAX_ITERATIONS:
        return "guard"
    return state.get("next", "END")


# ── Build the graph ───────────────────────────────────────────
def build_supervisor_graph() -> StateGraph:
    graph = StateGraph(TicketState)

    # Register nodes
    graph.add_node("triage_agent",    triage_agent)
    graph.add_node("security_agent",  security_agent)
    graph.add_node("diagnosis_agent", diagnosis_agent)
    graph.add_node("knowledge_agent", knowledge_agent)
    graph.add_node("human_review",    human_review_node)
    graph.add_node("guard",           guard_node)

    # Entry point
    graph.set_entry_point("triage_agent")

    # Edges with conditional routing
    for node in ["triage_agent", "security_agent", "diagnosis_agent",
                 "knowledge_agent", "human_review", "guard"]:
        graph.add_conditional_edges(
            node,
            route,
            {
                "triage_agent":    "triage_agent",
                "security_agent":  "security_agent",
                "diagnosis_agent": "diagnosis_agent",
                "knowledge_agent": "knowledge_agent",
                "human_review":    "human_review",
                "guard":           "guard",
                "END":             END,
            },
        )

    return graph


# ── Compiled graph singleton ──────────────────────────────────
#_checkpointer = MemorySaver()   # swap for SqliteSaver or RedisSaver in production
#_checkpointer = AsyncRedisSaver(redis_url=settings.redis_url)
#supervisor_graph = build_supervisor_graph().compile(
#    checkpointer=_checkpointer,
#    interrupt_before=["human_review"],   # pause BEFORE human_review executes
#)

@asynccontextmanager
async def get_supervisor_graph():
    """
    Async context manager yielding a compiled graph with a properly-scoped
    Redis checkpoint connection. Must be used as: async with get_supervisor_graph() as graph:
    """
    async with AsyncRedisSaver.from_conn_string(settings.redis_url) as checkpointer:
        await checkpointer.asetup()
        graph = build_supervisor_graph().compile(
            checkpointer=checkpointer,
        )
        yield graph

        
# ── Public entry point ────────────────────────────────────────
async def run_supervisor(ticket_id: str) -> dict:
    """
    Run the full supervisor graph for a given ticket ID.
    Fetches ticket from DB, builds initial state, runs graph.

    Returns the final TicketState dict.
    """
    from app.database import get_db_context
    from app.models.ticket import Ticket, TicketStatus, AssignedAgent
    from sqlalchemy import select

    # Fetch ticket
    async with get_db_context() as db:
        result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = result.scalar_one_or_none()

    if not ticket:
        return {"error": f"Ticket {ticket_id} not found"}

    # Defence in depth: the API rejects injection attempts at creation, but tickets
    # created before that check (or written outside the API) could still carry one.
    # Hand those to a human instead of sending the text to the LLM.
    from app.middleware.security import detect_prompt_injection
    if detect_prompt_injection(f"{ticket.title}\n{ticket.description}"):
        from loguru import logger
        logger.warning(f"Prompt injection suspected in {ticket.ticket_number}; agent run skipped")
        async with get_db_context() as db:
            result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
            t = result.scalar_one_or_none()
            if t:
                t.assigned_agent = AssignedAgent.HUMAN
        return {"error": "Possible prompt injection in ticket text; routed to a human for review."}

    # Build initial state
    initial_state: TicketState = {
        "ticket_id": str(ticket.id),
        "ticket_number": ticket.ticket_number,
        "title": ticket.title,
        "description": ticket.description,
        "category": ticket.category.value if ticket.category else "other",
        "priority": ticket.priority.value if ticket.priority else "medium",
        "created_by": str(ticket.created_by_id),
        "messages": [],
        "triage_result": {},
        "diagnosis_result": {},
        "knowledge_result": {},
        "security_result": {},
        "final_resolution": "",
        "next": "triage_agent",
        "iteration": 0,
        "hitl_required": False,
        "hitl_reason": "",
        "error": None,
        "langsmith_run_id": None,
    }

    config = {"configurable": {"thread_id": str(ticket.id)}}

    # Update ticket status to IN_PROGRESS
    async with get_db_context() as db:
        result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
        t = result.scalar_one_or_none()
        if t:
            t.status = TicketStatus.IN_PROGRESS
            t.assigned_agent = AssignedAgent.TRIAGE

    # Run the graph
    async with get_supervisor_graph() as graph:
        final_state = await graph.ainvoke(initial_state, config=config)

    await persist_final_state(ticket_id, final_state)
    return final_state


class NoPausedRunError(Exception):
    """Raised when /agents/resume is called for a ticket with no paused graph run."""


async def resume_supervisor(ticket_id: str, human_response: dict) -> dict:
    """
    Resume a graph paused at the human_review node and save the outcome to the ticket.

    human_response: {"approved": bool, "next_agent": str, "notes": str}
    Raises NoPausedRunError if there is no paused run for this ticket.
    """
    from langgraph.types import Command

    config = {"configurable": {"thread_id": str(ticket_id)}}
    async with get_supervisor_graph() as graph:
        snapshot = await graph.aget_state(config)
        if not snapshot or not snapshot.next:
            raise NoPausedRunError(f"No paused agent run for ticket {ticket_id}")
        final_state = await graph.ainvoke(Command(resume=human_response), config=config)

    await persist_final_state(
        ticket_id, final_state, human_rejected=not human_response.get("approved", False)
    )
    return final_state


async def persist_final_state(
    ticket_id: str, final_state: dict, human_rejected: bool = False
) -> None:
    """
    Write the outcome of a graph run (initial or resumed) back to the ticket.

    Outcomes:
      - paused again for human review → keep in_progress, assign to a human
      - rejected by the reviewer, or stopped with an error (e.g. the iteration
        guard) → keep in_progress, assign to a human; never auto-resolve
      - finished with a resolution → mark resolved
    Agent outputs and resolution notes are saved in every case, so nothing the
    agents produced is lost.
    """
    from datetime import datetime, timezone

    from app.database import get_db_context
    from app.models.ticket import AssignedAgent, Ticket, TicketStatus
    from sqlalchemy import select

    paused = bool(final_state.get("__interrupt__")) or bool(final_state.get("hitl_required"))
    needs_human = paused or human_rejected or bool(final_state.get("error"))

    async with get_db_context() as db:
        result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
        t = result.scalar_one_or_none()
        if not t:
            return

        if final_state.get("triage_result"):
            t.triage_output = final_state["triage_result"]
        if final_state.get("diagnosis_result"):
            t.diagnosis_output = final_state["diagnosis_result"]
        if final_state.get("final_resolution"):
            t.resolution_notes = final_state["final_resolution"]
        if final_state.get("langsmith_run_id"):
            t.langsmith_run_id = final_state["langsmith_run_id"]

        if needs_human or not final_state.get("final_resolution"):
            t.assigned_agent = AssignedAgent.HUMAN
        else:
            t.status = TicketStatus.RESOLVED
            t.resolved_at = datetime.now(timezone.utc)
