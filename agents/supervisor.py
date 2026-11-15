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
    #final_state = await supervisor_graph.ainvoke(initial_state, config=config)
    async with get_supervisor_graph() as graph:
        final_state = await graph.ainvoke(initial_state, config=config)
    # Update ticket with final outputs
    async with get_db_context() as db:
        result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
        t = result.scalar_one_or_none()
        if t:
            if final_state.get("final_resolution") and not final_state.get("hitl_required"):
                t.status = TicketStatus.RESOLVED
                from datetime import datetime, timezone
                t.resolved_at = datetime.now(timezone.utc)
            t.triage_output = final_state.get("triage_result")
            t.diagnosis_output = final_state.get("diagnosis_result")
            t.resolution_notes = final_state.get("final_resolution")
            if final_state.get("langsmith_run_id"):
                t.langsmith_run_id = final_state["langsmith_run_id"]

    return final_state
