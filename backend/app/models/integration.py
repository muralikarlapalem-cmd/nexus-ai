"""
NEXUS AI · app/models/integration.py
Tables for external ticketing systems.

    integration_connections   one row per connected system (e.g. "ServiceNow (prod)")
    external_ticket_links     which NEXUS ticket came from which external ticket
    integration_sync_runs     history of every sync, for the Integrations page

The tickets table itself is unchanged: a ticket "comes from" a source system
when it has a row in external_ticket_links.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class IntegrationConnection(Base):
    __tablename__ = "integration_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Connector settings. Never secrets: only names of INTEGRATION_* env vars.
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    auto_run_agents: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    write_back_notes: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    write_back_status: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Name of an INTEGRATION_* env var holding the webhook shared secret (empty = webhook off)
    webhook_secret_env: Mapped[str | None] = mapped_column(String(128))

    # Sync cursor: the newest "updated" time seen in the source system
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    links: Mapped[list["ExternalTicketLink"]] = relationship(back_populates="connection")
    sync_runs: Mapped[list["IntegrationSyncRun"]] = relationship(
        back_populates="connection", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<IntegrationConnection {self.name} [{self.connector_type}]>"


class ExternalTicketLink(Base):
    __tablename__ = "external_ticket_links"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_external_ticket_per_connection"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), unique=True, nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_connections.id"), nullable=False, index=True)

    # Copied from the connection so ticket lists can show the source without a second join
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)

    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_number: Mapped[str | None] = mapped_column(String(100), index=True)
    external_url: Mapped[str | None] = mapped_column(Text)
    external_status: Mapped[str | None] = mapped_column(String(30))   # last status seen, NEXUS terms
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(String(64))

    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Write-back bookkeeping (so the same note is never posted twice)
    last_note_hash: Mapped[str | None] = mapped_column(String(64))
    last_written_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_write_back_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    connection: Mapped["IntegrationConnection"] = relationship(back_populates="links")
    ticket: Mapped["Ticket"] = relationship(back_populates="external_link")  # noqa: F821

    def __repr__(self) -> str:
        return f"<ExternalTicketLink {self.source_name}:{self.external_number or self.external_id}>"


class IntegrationSyncRun(Base):
    __tablename__ = "integration_sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)      # schedule | manual | webhook
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    # running | success | partial | failed | skipped

    fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    agents_queued: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSONB)          # per-ticket errors, capped

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    connection: Mapped["IntegrationConnection"] = relationship(back_populates="sync_runs")
