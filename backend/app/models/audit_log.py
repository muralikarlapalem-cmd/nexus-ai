"""
NEXUS AI · app/models/audit_log.py
Immutable audit trail — every significant action is recorded here.
Used for compliance, security review, and fine-tuning data collection.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Who did it
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    user: Mapped["User | None"] = relationship("User", back_populates="audit_logs")  # noqa: F821

    # What they did
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # e.g. "ticket.create", "ticket.status_change", "agent.triage", "auth.login"

    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # e.g. "ticket", "user", "runbook"

    resource_id: Mapped[str | None] = mapped_column(String(255), index=True)

    # Details — flexible JSONB payload
    details: Mapped[dict | None] = mapped_column(JSONB)
    # e.g. {"old_status": "open", "new_status": "triaged", "agent": "triage_agent"}

    # Agent trace context (Phase 5)
    trace_id: Mapped[str | None] = mapped_column(String(255))
    span_id: Mapped[str | None] = mapped_column(String(255))

    # Request metadata
    ip_address: Mapped[str | None] = mapped_column(String(45))    # IPv4 or IPv6
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(36))    # X-Request-ID

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} on {self.resource_type}/{self.resource_id}>"
