"""
NEXUS AI · app/api/v1/tickets.py
Ticket CRUD + status transitions.
Phase 1 stores everything in Postgres.
Phase 3 will hook agent invocation here after ticket creation.
"""

import math
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_user, require_agent_or_admin
from app.database import get_async_db
from app.middleware.security import check_input_safety
from app.models.audit_log import AuditLog
from app.models.ticket import Ticket, TicketPriority, TicketStatus, TicketStatusHistory, VALID_STATUS_TRANSITIONS
from app.models.user import User, UserRole
from app.schemas.ticket import (
    TicketCreate,
    TicketListResponse,
    TicketResponse,
    TicketStatusChange,
    TicketUpdate,
)
from app.workers.celery_app import process_ticket_task

router = APIRouter(prefix="/tickets", tags=["Tickets"])

# ── SLA windows per priority ──────────────────────────────────
SLA_HOURS: dict[TicketPriority, int] = {
    TicketPriority.CRITICAL: 4,
    TicketPriority.HIGH: 8,
    TicketPriority.MEDIUM: 24,
    TicketPriority.LOW: 72,
}


def _generate_ticket_number(sequence: int) -> str:
    """NX-YYYY-NNNNN format e.g. NX-2025-00042"""
    year = datetime.now(timezone.utc).year
    return f"NX-{year}-{sequence:05d}"


@router.post("/", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    body: TicketCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a new IT ticket.
    After creation, dispatches async Celery task to trigger the Triage Agent (Phase 3).
    """
    # Ticket text is fed to every agent's LLM prompt and later ingested into RAG
    check_input_safety(f"{body.title}\n{body.description}", source="ticket")

    # Generate sequential ticket number
    count_result = await db.execute(select(func.count()).select_from(Ticket))
    seq = (count_result.scalar_one() or 0) + 1

    ticket = Ticket(
        ticket_number=_generate_ticket_number(seq),
        title=body.title,
        description=body.description,
        category=body.category,
        priority=body.priority,
        created_by_id=current_user.id,
        sla_due_at=datetime.now(timezone.utc) + timedelta(hours=SLA_HOURS[body.priority]),
    )
    db.add(ticket)
    await db.flush()   # get the UUID before commit

    # Audit
    db.add(AuditLog(
        user_id=current_user.id,
        action="ticket.create",
        resource_type="ticket",
        resource_id=str(ticket.id),
        details={"ticket_number": ticket.ticket_number, "priority": body.priority.value},
    ))

    await db.refresh(ticket, ["created_by_user"])

    # ── Phase 3 hook: dispatch Celery task for agent processing ──
    # Uncomment when Phase 3 agents are ready:
    # process_ticket_task.delay(str(ticket.id))

    logger.info(f"Ticket created: {ticket.ticket_number} by {current_user.username}")
    return ticket


@router.get("/", response_model=TicketListResponse)
async def list_tickets(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    q: str | None = Query(None, max_length=200, description="Search ticket number, title or description"),
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    """
    List tickets with pagination and optional filters.
    `q` matches the ticket number, title or description (case-insensitive).
    Regular users only see their own tickets; agents/admins see all.
    """
    query = select(Ticket).options(selectinload(Ticket.created_by_user))

    # RBAC filtering
    if current_user.role == UserRole.USER:
        query = query.where(Ticket.created_by_id == current_user.id)

    if status:
        query = query.where(Ticket.status == status)
    if priority:
        query = query.where(Ticket.priority == priority)
    if q and q.strip():
        # Escape LIKE wildcards so "%" or "_" in the search box are matched literally
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{term}%"
        query = query.where(or_(
            Ticket.ticket_number.ilike(pattern, escape="\\"),
            Ticket.title.ilike(pattern, escape="\\"),
            Ticket.description.ilike(pattern, escape="\\"),
        ))

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    # Paginate
    offset = (page - 1) * page_size
    result = await db.execute(
        # Tie-break on ticket_number so tickets created in the same instant never
        # shift between pages
        query.order_by(Ticket.created_at.desc(), Ticket.ticket_number.desc())
        .offset(offset).limit(page_size)
    )
    tickets = result.scalars().all()

    return TicketListResponse(
        items=tickets,
        total=total,
        page=page,
        page_size=page_size,
        pages=math.ceil(total / page_size) if total else 0,
    )


@router.get("/{ticket_id}", response_model=TicketResponse)
async def get_ticket(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch a single ticket by UUID."""
    result = await db.execute(
        select(Ticket)
        .options(selectinload(Ticket.created_by_user))
        .where(Ticket.id == ticket_id)
    )
    ticket: Ticket | None = result.scalar_one_or_none()

    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    # Regular users can only see their own
    if current_user.role == UserRole.USER and ticket.created_by_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return ticket


@router.patch("/{ticket_id}", response_model=TicketResponse)
async def update_ticket(
    ticket_id: uuid.UUID,
    body: TicketUpdate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_agent_or_admin),
):
    """
    Partial update — agents and admins only.
    Used by agent subsystems (Phase 3) to write back their outputs.
    """
    result = await db.execute(
        select(Ticket).options(selectinload(Ticket.created_by_user)).where(Ticket.id == ticket_id)
    )
    ticket: Ticket | None = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    # Resolution notes are ingested into the RAG knowledge base
    if body.resolution_notes:
        check_input_safety(body.resolution_notes, source="resolution_notes")

    update_data = body.model_dump(exclude_none=True)
    old_status = ticket.status

    for field, value in update_data.items():
        setattr(ticket, field, value)

    # Timestamp triggers
    if body.status == TicketStatus.TRIAGED and not ticket.triaged_at:
        ticket.triaged_at = datetime.now(timezone.utc)
    if body.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED) and not ticket.resolved_at:
        ticket.resolved_at = datetime.now(timezone.utc)

    db.add(AuditLog(
        user_id=current_user.id,
        action="ticket.update",
        resource_type="ticket",
        resource_id=str(ticket.id),
        details={**update_data, "old_status": old_status.value if old_status else None},
    ))

    return ticket


@router.post("/{ticket_id}/status", response_model=TicketResponse)
async def change_status(
    ticket_id: uuid.UUID,
    body: TicketStatusChange,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_agent_or_admin),
):
    """Explicit status change endpoint with optional note."""
    result = await db.execute(
        select(Ticket).options(selectinload(Ticket.created_by_user)).where(Ticket.id == ticket_id)
    )
    ticket: Ticket | None = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    old_status = ticket.status

    # Validate the transition is allowed
    allowed_next = VALID_STATUS_TRANSITIONS.get(old_status, set())
    if body.status not in allowed_next and body.status != old_status:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot change status from '{old_status.value}' to '{body.status.value}'",
        )

    ticket.status = body.status
    if body.note:
        # The note becomes resolution_notes, which is ingested into RAG
        check_input_safety(body.note, source="status_note")
        ticket.resolution_notes = body.note

    if body.status == TicketStatus.RESOLVED and not ticket.resolved_at:
        ticket.resolved_at = datetime.now(timezone.utc)

    # Write to status history for audit trail
    db.add(TicketStatusHistory(
        ticket_id=ticket.id,
        old_status=old_status,
        new_status=body.status,
        changed_by_id=current_user.id,
        reason=body.note,
    ))

    db.add(AuditLog(
        user_id=current_user.id,
        action="ticket.status_change",
        resource_type="ticket",
        resource_id=str(ticket.id),
        details={"old_status": old_status.value, "new_status": body.status.value, "note": body.note},
    ))

    logger.info(f"Ticket {ticket.ticket_number}: {old_status} → {body.status} by {current_user.username}")
    return ticket