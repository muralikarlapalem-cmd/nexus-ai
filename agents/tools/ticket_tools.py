"""
NEXUS AI · agents/tools/ticket_tools.py
Ticket management tools — agents use these to read ticket data,
update status, and write back their outputs to Postgres.
"""

from langchain_core.tools import tool


@tool
async def get_ticket_details(ticket_id: str) -> str:
    """
    Fetch full ticket details from the database by ticket ID (UUID).
    Returns a formatted string with all ticket fields.
    """
    try:
        from app.database import get_db_context
        from app.models.ticket import Ticket
        from sqlalchemy import select

        async with get_db_context() as db:
            result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
            ticket = result.scalar_one_or_none()

        if not ticket:
            return f"Ticket {ticket_id} not found."

        return (
            f"Ticket: {ticket.ticket_number}\n"
            f"Title: {ticket.title}\n"
            f"Description: {ticket.description}\n"
            f"Category: {ticket.category.value}\n"
            f"Priority: {ticket.priority.value}\n"
            f"Status: {ticket.status.value}\n"
            f"Created: {ticket.created_at}\n"
            f"SLA Due: {ticket.sla_due_at}\n"
            f"SLA Breached: {ticket.sla_breached}"
        )
    except Exception as e:
        return f"Error fetching ticket: {str(e)}"


@tool
async def update_ticket_field(ticket_id: str, field: str, value: str) -> str:
    """
    Update a single field on a ticket (agents write their outputs here).
    Allowed fields: resolution_notes, triage_output, diagnosis_output.

    Args:
        ticket_id: UUID of the ticket
        field: One of: resolution_notes, triage_output, diagnosis_output
        value: New value (JSON string for dict fields)
    """
    ALLOWED_FIELDS = {"resolution_notes", "triage_output", "diagnosis_output"}
    if field not in ALLOWED_FIELDS:
        return f"Field '{field}' is not updatable by agents. Allowed: {ALLOWED_FIELDS}"

    try:
        import json
        from app.database import get_db_context
        from app.models.ticket import Ticket
        from sqlalchemy import select

        parsed_value = json.loads(value) if field.endswith("_output") else value

        async with get_db_context() as db:
            result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
            ticket = result.scalar_one_or_none()
            if not ticket:
                return f"Ticket {ticket_id} not found."
            setattr(ticket, field, parsed_value)

        return f"Successfully updated {field} on ticket {ticket_id}"
    except Exception as e:
        return f"Error updating ticket: {str(e)}"


@tool
async def check_sla_status(ticket_id: str) -> str:
    """
    Check the SLA status of a ticket — how much time is remaining or how much it has been breached.
    """
    try:
        from datetime import datetime, timezone
        from app.database import get_db_context
        from app.models.ticket import Ticket
        from sqlalchemy import select

        async with get_db_context() as db:
            result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
            ticket = result.scalar_one_or_none()

        if not ticket or not ticket.sla_due_at:
            return "SLA information not available for this ticket."

        now = datetime.now(timezone.utc)
        delta = ticket.sla_due_at - now

        if delta.total_seconds() > 0:
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            return f"SLA OK — {hours}h {minutes}m remaining (due: {ticket.sla_due_at})"
        else:
            breached_hours = abs(int(delta.total_seconds() // 3600))
            return f"SLA BREACHED — {breached_hours}h overdue (was due: {ticket.sla_due_at})"
    except Exception as e:
        return f"Error checking SLA: {str(e)}"
