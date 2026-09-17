"""
NEXUS AI · app/models/ticket.py
IT Ticket — the primary domain object that flows through all agents.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TicketStatus(str, enum.Enum):
    OPEN = "open"
    TRIAGED = "triaged"           # Triage Agent processed
    IN_PROGRESS = "in_progress"   # Diagnosis Agent working
    AWAITING_USER = "awaiting_user"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"

VALID_STATUS_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.OPEN: {TicketStatus.TRIAGED, TicketStatus.CLOSED, TicketStatus.CANCELLED},
    TicketStatus.TRIAGED: {TicketStatus.IN_PROGRESS, TicketStatus.CANCELLED},
    TicketStatus.IN_PROGRESS: {TicketStatus.AWAITING_USER, TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.CANCELLED},
    TicketStatus.AWAITING_USER: {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED, TicketStatus.CANCELLED},
    TicketStatus.RESOLVED: set(),     # terminal — no path to CLOSED per your rule
    TicketStatus.CLOSED: set(),       # terminal
    TicketStatus.CANCELLED: set(),    # terminal
}

class TicketPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TicketCategory(str, enum.Enum):
    NETWORK = "network"
    HARDWARE = "hardware"
    SOFTWARE = "software"
    SECURITY = "security"
    ACCESS = "access"
    PERFORMANCE = "performance"
    DATABASE = "database"
    OTHER = "other"


class AssignedAgent(str, enum.Enum):
    TRIAGE = "triage_agent"
    DIAGNOSIS = "diagnosis_agent"
    KNOWLEDGE = "knowledge_agent"
    SECURITY = "security_agent"
    ANALYTICS = "analytics_agent"
    HUMAN = "human"
    UNASSIGNED = "unassigned"


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_number: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, index=True
        # e.g. NX-2025-00001 — generated via DB trigger or service layer
    )

    # Content
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Classification (set by Triage Agent)
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="ticket_status"),
        default=TicketStatus.OPEN, nullable=False, index=True
    )
    priority: Mapped[TicketPriority] = mapped_column(
        Enum(TicketPriority, name="ticket_priority"),
        default=TicketPriority.MEDIUM, nullable=False, index=True
    )
    category: Mapped[TicketCategory] = mapped_column(
        Enum(TicketCategory, name="ticket_category"),
        default=TicketCategory.OTHER, nullable=False
    )
    assigned_agent: Mapped[AssignedAgent] = mapped_column(
        Enum(AssignedAgent, name="assigned_agent"),
        default=AssignedAgent.UNASSIGNED, nullable=False
    )

    # Agent outputs (stored as JSONB for flexibility)
    triage_output: Mapped[dict | None] = mapped_column(JSONB)
    diagnosis_output: Mapped[dict | None] = mapped_column(JSONB)
    resolution_notes: Mapped[str | None] = mapped_column(Text)

    # Vector store reference (populated by Knowledge Agent in Phase 2)
    embedding_id: Mapped[str | None] = mapped_column(String(255))

    # LangGraph trace reference (populated in Phase 5)
    langsmith_run_id: Mapped[str | None] = mapped_column(String(255))

    # Relationships
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_by_user: Mapped["User"] = relationship(  # noqa: F821
        "User", back_populates="tickets", foreign_keys=[created_by_id]
    )

    status_history: Mapped[list["TicketStatusHistory"]] = relationship(
        "TicketStatusHistory", back_populates="ticket",
        order_by="TicketStatusHistory.changed_at", cascade="all, delete-orphan"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # SLA tracking
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sla_breached: Mapped[bool] = mapped_column(default=False)

    def __repr__(self) -> str:
        return f"<Ticket {self.ticket_number} [{self.status}/{self.priority}]>"

    @property
    def is_open(self) -> bool:
        return self.status in (TicketStatus.OPEN, TicketStatus.TRIAGED, TicketStatus.IN_PROGRESS)

class TicketStatusHistory(Base):
    __tablename__ = "ticket_status_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False, index=True
    )
    old_status: Mapped[TicketStatus | None] = mapped_column(
        Enum(TicketStatus, name="ticket_status")
    )
    new_status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="ticket_status"), nullable=False
    )
    changed_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    changed_by_user: Mapped["User"] = relationship("User", foreign_keys=[changed_by_id])  # noqa: F821
    reason: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="status_history")

    def __repr__(self) -> str:
        return f"<TicketStatusHistory {self.ticket_id}: {self.old_status}→{self.new_status}>"


