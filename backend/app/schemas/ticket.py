"""
NEXUS AI · app/schemas/ticket.py
Pydantic v2 schemas for Ticket — request validation + response serialization.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.ticket import AssignedAgent, TicketCategory, TicketPriority, TicketStatus


# ── Request schemas ───────────────────────────────────────────

class TicketCreate(BaseModel):
    """Submitted by the end user to open a new ticket."""
    title: str = Field(..., min_length=5, max_length=500)
    description: str = Field(..., min_length=10, max_length=10_000)
    category: TicketCategory = TicketCategory.OTHER
    priority: TicketPriority = TicketPriority.MEDIUM   # user hint; Triage Agent may override

    @field_validator("title", "description")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class TicketUpdate(BaseModel):
    """Partial updates — agents and admins only."""
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    category: TicketCategory | None = None
    assigned_agent: AssignedAgent | None = None
    resolution_notes: str | None = None
    triage_output: dict | None = None
    diagnosis_output: dict | None = None


# ── Response schemas ──────────────────────────────────────────

class UserSummary(BaseModel):
    id: uuid.UUID
    username: str
    full_name: str | None

    model_config = {"from_attributes": True}


class TicketResponse(BaseModel):
    """Full ticket representation returned by the API."""
    id: uuid.UUID
    ticket_number: str
    title: str
    description: str
    status: TicketStatus
    priority: TicketPriority
    category: TicketCategory
    assigned_agent: AssignedAgent
    resolution_notes: str | None
    triage_output: dict | None
    diagnosis_output: dict | None
    sla_breached: bool
    sla_due_at: datetime | None
    created_by: UserSummary = Field(validation_alias="created_by_user")
    created_at: datetime
    updated_at: datetime
    triaged_at: datetime | None
    resolved_at: datetime | None
    langsmith_run_id: str | None

    model_config = {"from_attributes": True}


class TicketListResponse(BaseModel):
    """Paginated list of tickets."""
    items: list[TicketResponse]
    total: int
    page: int
    page_size: int
    pages: int


class TicketStatusChange(BaseModel):
    """Lightweight payload for status transitions."""
    status: TicketStatus
    note: str | None = Field(None, max_length=2000)
