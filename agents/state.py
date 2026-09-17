"""
NEXUS AI · agents/state.py
Shared LangGraph state — the single source of truth flowing through every node.
Every agent reads from and writes back to this TypedDict.
"""

from typing import Annotated, Any
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class TicketState(TypedDict):
    """
    The complete state object that travels through the LangGraph supervisor.

    Design rules:
    - Agents only append to `messages` (never overwrite)
    - Each agent writes its output to its dedicated field
    - `next` is set by the supervisor to route to the next agent
    - `hitl_required` triggers a human checkpoint before proceeding
    """

    # ── Core ticket data ──────────────────────────────────────
    ticket_id: str
    ticket_number: str
    title: str
    description: str
    category: str
    priority: str
    created_by: str

    # ── Conversation history (append-only via add_messages) ───
    messages: Annotated[list[BaseMessage], add_messages]

    # ── Agent outputs ─────────────────────────────────────────
    triage_result: dict[str, Any]       # Triage Agent output
    diagnosis_result: dict[str, Any]    # Diagnosis Agent output
    knowledge_result: dict[str, Any]    # Knowledge Agent output
    security_result: dict[str, Any]     # Security Agent output
    final_resolution: str               # Compiled final answer

    # ── Orchestration control ─────────────────────────────────
    next: str                           # Next node to execute
    iteration: int                      # Guard against infinite loops
    hitl_required: bool                 # Human-in-the-loop checkpoint
    hitl_reason: str                    # Why HITL was triggered
    error: str | None                   # Propagate errors cleanly

    # ── Tracing ───────────────────────────────────────────────
    langsmith_run_id: str | None        # Written back to Postgres (Phase 5)
