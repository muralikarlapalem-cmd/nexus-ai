"""
NEXUS AI · app/integrations/sync.py
The sync engine. Works the same for every connector.

    sync_connection()   pull changed tickets → upsert into NEXUS → queue agents for new ones
    process_webhook()   same upsert, for tickets pushed by the source system
    write_back_ticket() post the agents' result to the source as an internal note

Rules that keep it safe to run repeatedly:
  - Each external ticket maps to exactly one NEXUS ticket (unique per connection).
  - Unchanged tickets are skipped by content hash, so NEXUS's own work notes
    (which bump the source's "updated" time) never cause re-imports.
  - The source's status is mirrored only when the source's status itself
    changes, so a ticket NEXUS resolved isn't reopened by an unrelated edit.
  - Each ticket is saved in its own transaction: one bad record doesn't fail the batch.
  - A connection never syncs twice at the same time (Postgres advisory lock).
  - Agents run only on tickets that arrive open, and only after they're saved.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal, get_db_context
from app.integrations.base import ConnectorError, TicketConnector
from app.integrations.registry import build_connector
from app.integrations.schemas import ExternalTicket
from app.models.audit_log import AuditLog
from app.models.integration import ExternalTicketLink, IntegrationConnection, IntegrationSyncRun
from app.models.ticket import (
    AssignedAgent, Ticket, TicketCategory, TicketPriority, TicketStatus, TicketStatusHistory,
)
from app.models.user import User, UserRole

# Re-read this much before the cursor, so clock differences can't skip a ticket
SYNC_OVERLAP = timedelta(minutes=2)
MAX_TICKETS_PER_RUN = 500
MAX_ERRORS_KEPT = 20

INTEGRATION_USERNAME = "integrations"

SLA_HOURS: dict[TicketPriority, int] = {
    TicketPriority.CRITICAL: 4,
    TicketPriority.HIGH: 8,
    TicketPriority.MEDIUM: 24,
    TicketPriority.LOW: 72,
}

TERMINAL = {TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.CANCELLED}

AgentDispatcher = Callable[[str], None]


def _default_dispatch(ticket_id: str) -> None:
    from app.workers.celery_app import process_ticket_task
    process_ticket_task.delay(ticket_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_key(connection_id: uuid.UUID) -> int:
    return int.from_bytes(connection_id.bytes[:8], "big", signed=True)


# ─────────────────────────────────────────────────────────────
#  Small helpers
# ─────────────────────────────────────────────────────────────
async def get_integration_user(db) -> User:
    """
    Imported tickets need a creator. This service account can't log in:
    it has no password and is marked inactive.
    """
    result = await db.execute(select(User).where(User.username == INTEGRATION_USERNAME))
    user = result.scalar_one_or_none()
    if user:
        return user
    user = User(
        email="integrations@nexus.local", username=INTEGRATION_USERNAME,
        full_name="External systems", role=UserRole.AGENT,
        hashed_password=None, is_active=False,
    )
    db.add(user)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:            # created by a parallel sync a moment ago
        result = await db.execute(select(User).where(User.username == INTEGRATION_USERNAME))
        user = result.scalar_one()
    return user


async def next_ticket_number(db) -> str:
    """NX-YYYY-NNNNN, continuing from the highest number this year (and never below count + 1)."""
    year = _now().year
    prefix = f"NX-{year}-"
    highest = (await db.execute(
        select(func.max(Ticket.ticket_number)).where(Ticket.ticket_number.like(f"{prefix}%"))
    )).scalar_one_or_none()
    count = (await db.execute(select(func.count()).select_from(Ticket))).scalar_one() or 0
    seq = count + 1
    if highest:
        try:
            seq = max(seq, int(highest.rsplit("-", 1)[1]) + 1)
        except ValueError:
            pass
    return f"{prefix}{seq:05d}"


def _connector_for(connection: IntegrationConnection) -> TicketConnector:
    return build_connector(connection.connector_type, connection.config or {})


# ─────────────────────────────────────────────────────────────
#  Upsert one external ticket
# ─────────────────────────────────────────────────────────────
async def upsert_external_ticket(
    db, connection: IntegrationConnection, ext: ExternalTicket, bot: User
) -> tuple[str, Ticket]:
    """Returns ("created" | "updated" | "unchanged", ticket)."""
    link = (await db.execute(
        select(ExternalTicketLink).where(
            ExternalTicketLink.connection_id == connection.id,
            ExternalTicketLink.external_id == ext.external_id,
        )
    )).scalar_one_or_none()
    content_hash = ext.content_hash()
    now = _now()
    label = ext.external_number or ext.external_id

    if link is None:
        priority = TicketPriority(ext.priority)
        status = TicketStatus(ext.status)
        created_at = ext.created_at or now
        ticket = Ticket(
            title=ext.title,
            description=ext.description or ext.title,
            priority=priority,
            category=TicketCategory(ext.category),
            status=status,
            created_by_id=bot.id,
            created_at=created_at,
            sla_due_at=created_at + timedelta(hours=SLA_HOURS[priority]),
            resolved_at=(ext.updated_at or now) if status in (TicketStatus.RESOLVED, TicketStatus.CLOSED) else None,
        )
        # Ticket numbers are unique; retry if a ticket was created at the same moment
        for attempt in range(3):
            ticket.ticket_number = await next_ticket_number(db)
            try:
                async with db.begin_nested():
                    db.add(ticket)
                    await db.flush()
                break
            except IntegrityError:
                if attempt == 2:
                    raise
        db.add(ExternalTicketLink(
            ticket_id=ticket.id, connection_id=connection.id,
            source_type=connection.connector_type, source_name=connection.name,
            external_id=ext.external_id, external_number=ext.external_number,
            external_url=ext.external_url, external_status=ext.status,
            external_updated_at=ext.updated_at, content_hash=content_hash, last_synced_at=now,
        ))
        db.add(TicketStatusHistory(
            ticket_id=ticket.id, old_status=None, new_status=status, changed_by_id=bot.id,
            reason=f"Imported from {connection.name} ({label})",
        ))
        db.add(AuditLog(
            user_id=None, action="integration.ticket_import", resource_type="ticket",
            resource_id=str(ticket.id),
            details={"connection": connection.name, "external": label, "ticket_number": ticket.ticket_number},
        ))
        return "created", ticket

    ticket = (await db.execute(select(Ticket).where(Ticket.id == link.ticket_id))).scalar_one()
    link.last_synced_at = now
    link.external_updated_at = ext.updated_at or link.external_updated_at
    if ext.external_url:
        link.external_url = ext.external_url
    if link.content_hash == content_hash:
        return "unchanged", ticket

    # Content from the source wins for the fields it owns
    ticket.title = ext.title
    ticket.description = ext.description or ext.title
    ticket.priority = TicketPriority(ext.priority)
    ticket.category = TicketCategory(ext.category)

    # Mirror status only when the source's status changed since the last sync
    if ext.status != link.external_status:
        new_status = TicketStatus(ext.status)
        if new_status != ticket.status:
            db.add(TicketStatusHistory(
                ticket_id=ticket.id, old_status=ticket.status, new_status=new_status,
                changed_by_id=bot.id, reason=f"Status changed in {connection.name} ({label})",
            ))
            ticket.status = new_status
            if new_status in (TicketStatus.RESOLVED, TicketStatus.CLOSED) and not ticket.resolved_at:
                ticket.resolved_at = ext.updated_at or now
        link.external_status = ext.status

    link.content_hash = content_hash
    link.external_number = ext.external_number or link.external_number
    return "updated", ticket


# ─────────────────────────────────────────────────────────────
#  Apply a batch (used by polling and webhooks)
# ─────────────────────────────────────────────────────────────
async def _apply_batch(
    connection_id: uuid.UUID, tickets: list[ExternalTicket], run: dict[str, Any]
) -> tuple[list[str], list[ExternalTicket]]:
    """Upsert each ticket in its own transaction. Returns (ticket ids to run agents on, failed tickets)."""
    to_run: list[str] = []
    failed: list[ExternalTicket] = []
    for ext in tickets:
        try:
            async with get_db_context() as db:
                connection = await db.get(IntegrationConnection, connection_id)
                bot = await get_integration_user(db)
                outcome, ticket = await upsert_external_ticket(db, connection, ext, bot)
                run[outcome] += 1
                if outcome == "created" and connection.auto_run_agents and ticket.status == TicketStatus.OPEN:
                    to_run.append(str(ticket.id))
        except Exception as e:     # one bad record must not stop the batch
            logger.exception(f"Sync: could not save {ext.external_number or ext.external_id}")
            run["failed"] += 1
            failed.append(ext)
            if len(run["errors"]) < MAX_ERRORS_KEPT:
                run["errors"].append({"external": ext.external_number or ext.external_id, "error": str(e)[:300]})
    return to_run, failed


def _dispatch(ticket_ids: list[str], dispatch: AgentDispatcher | None) -> int:
    dispatch = dispatch or _default_dispatch
    queued = 0
    for ticket_id in ticket_ids:
        try:
            dispatch(ticket_id)
            queued += 1
        except Exception as e:      # broker down: the ticket stays open; "Run Agents" still works
            logger.warning(f"Sync: could not queue agents for ticket {ticket_id}: {e}")
    return queued


async def _start_run(connection_id: uuid.UUID, trigger: str) -> uuid.UUID:
    async with get_db_context() as db:
        run = IntegrationSyncRun(connection_id=connection_id, trigger=trigger, status="running")
        db.add(run)
        await db.flush()
        return run.id


async def _finish_run(run_id: uuid.UUID, counts: dict[str, Any], status: str, error: str | None) -> dict[str, Any]:
    async with get_db_context() as db:
        run = await db.get(IntegrationSyncRun, run_id)
        for key in ("fetched", "created", "updated", "unchanged", "failed", "agents_queued"):
            setattr(run, key, counts.get(key, 0))
        run.status = status
        run.error = error
        run.details = {"errors": counts.get("errors", [])} if counts.get("errors") else None
        run.finished_at = _now()
        return run_summary(run)


def run_summary(run: IntegrationSyncRun) -> dict[str, Any]:
    return {
        "id": str(run.id), "connection_id": str(run.connection_id), "trigger": run.trigger,
        "status": run.status, "fetched": run.fetched, "created": run.created, "updated": run.updated,
        "unchanged": run.unchanged, "failed": run.failed, "agents_queued": run.agents_queued,
        "error": run.error, "details": run.details,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def _new_counts() -> dict[str, Any]:
    return {"fetched": 0, "created": 0, "updated": 0, "unchanged": 0, "failed": 0,
            "agents_queued": 0, "errors": []}


# ─────────────────────────────────────────────────────────────
#  Public: poll a connection
# ─────────────────────────────────────────────────────────────
async def sync_connection(
    connection_id: uuid.UUID | str,
    trigger: str = "manual",
    full: bool = False,
    dispatch: AgentDispatcher | None = None,
) -> dict[str, Any] | None:
    """
    Pull tickets changed since the last sync and upsert them.
    full=True ignores the cursor (and the first-sync history limit stays in force).
    Returns the run summary, or None if the connection is missing or disabled for a scheduled run.
    """
    connection_id = uuid.UUID(str(connection_id))
    async with get_db_context() as db:
        connection = await db.get(IntegrationConnection, connection_id)
        if connection is None:
            return None
        if trigger == "schedule" and not connection.enabled:
            return None

    # One sync per connection at a time. The lock lives on its own DB connection
    # and is released when that connection closes, even if this process dies.
    async with AsyncSessionLocal() as lock_session:
        got_lock = (await lock_session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _lock_key(connection_id)}
        )).scalar()
        if not got_lock:
            run_id = await _start_run(connection_id, trigger)
            return await _finish_run(run_id, _new_counts(), "skipped", "Another sync of this connection is running")
        try:
            return await _sync_locked(connection_id, trigger, full, dispatch)
        finally:
            await lock_session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _lock_key(connection_id)})
            await lock_session.commit()


async def _sync_locked(connection_id, trigger, full, dispatch) -> dict[str, Any]:
    run_id = await _start_run(connection_id, trigger)
    counts = _new_counts()

    async with get_db_context() as db:
        connection = await db.get(IntegrationConnection, connection_id)
        cursor = connection.last_synced_at
        connector = _connector_for(connection)

    since = None if (full or cursor is None) else cursor - SYNC_OVERLAP
    try:
        tickets = await connector.fetch_updated(since, limit=MAX_TICKETS_PER_RUN)
    except ConnectorError as e:
        return await _record_failure(connection_id, run_id, counts, str(e))
    except Exception as e:
        logger.exception("Sync: unexpected error while fetching")
        return await _record_failure(connection_id, run_id, counts, f"Unexpected error: {e}")

    counts["fetched"] = len(tickets)
    to_run, failed = await _apply_batch(connection_id, tickets, counts)

    # Move the cursor to the newest change seen. If some tickets failed, stop just
    # before the oldest failure so the next sync tries them again.
    times = [t.updated_at for t in tickets if t.updated_at]
    failed_times = [t.updated_at for t in failed if t.updated_at]
    new_cursor = cursor
    if failed_times:
        new_cursor = min(failed_times) - timedelta(seconds=1)
    elif times:
        new_cursor = max(times)
    if cursor and new_cursor and new_cursor < cursor and not failed_times:
        new_cursor = cursor

    status = "success" if counts["failed"] == 0 else ("failed" if counts["failed"] == len(tickets) else "partial")
    error = None if status == "success" else f"{counts['failed']} ticket(s) could not be saved"
    async with get_db_context() as db:
        connection = await db.get(IntegrationConnection, connection_id)
        connection.last_synced_at = new_cursor
        connection.last_sync_status = status
        connection.last_error = error

    counts["agents_queued"] = _dispatch(to_run, dispatch)
    summary = await _finish_run(run_id, counts, status, error)
    logger.info(f"Sync {connection_id}: {summary['status']} fetched={counts['fetched']} "
                f"created={counts['created']} updated={counts['updated']} unchanged={counts['unchanged']} "
                f"failed={counts['failed']} agents={counts['agents_queued']}")
    return summary


async def _record_failure(connection_id, run_id, counts, message: str) -> dict[str, Any]:
    logger.warning(f"Sync {connection_id} failed: {message}")
    async with get_db_context() as db:
        connection = await db.get(IntegrationConnection, connection_id)
        connection.last_sync_status = "failed"
        connection.last_error = message
    return await _finish_run(run_id, counts, "failed", message)


# ─────────────────────────────────────────────────────────────
#  Public: webhook push
# ─────────────────────────────────────────────────────────────
async def process_webhook(
    connection_id: uuid.UUID | str, payload: dict[str, Any], dispatch: AgentDispatcher | None = None
) -> dict[str, Any]:
    connection_id = uuid.UUID(str(connection_id))
    run_id = await _start_run(connection_id, "webhook")
    counts = _new_counts()
    async with get_db_context() as db:
        connection = await db.get(IntegrationConnection, connection_id)
        connector = _connector_for(connection)
    try:
        tickets = connector.parse_webhook(payload)
    except Exception as e:
        return await _finish_run(run_id, counts, "failed", f"Could not read the webhook payload: {e}")
    counts["fetched"] = len(tickets)
    to_run, _ = await _apply_batch(connection_id, tickets, counts)
    counts["agents_queued"] = _dispatch(to_run, dispatch)
    status = "success" if counts["failed"] == 0 else "partial"
    return await _finish_run(run_id, counts, status, None if status == "success" else "Some tickets failed")


# ─────────────────────────────────────────────────────────────
#  Public: write the agents' result back to the source
# ─────────────────────────────────────────────────────────────
def _pct(value: Any) -> str:
    try:
        v = float(value)
        return f"{v:.0%}" if v <= 1 else f"{v:.0f}%"
    except (TypeError, ValueError):
        return ""


def build_note(ticket: Ticket, external_label: str) -> str | None:
    """Plain-text work note summarising what the agents found. None if they've produced nothing yet."""
    from app.config import settings

    triage = ticket.triage_output or {}
    diagnosis = ticket.diagnosis_output or {}
    needs_human = ticket.assigned_agent == AssignedAgent.HUMAN and ticket.status not in TERMINAL
    if not (triage or diagnosis or ticket.resolution_notes or needs_human):
        return None

    lines = [f"[NEXUS AI] Automated analysis for {external_label} (NEXUS ticket {ticket.ticket_number})", ""]
    if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        lines.append("Outcome: suggested resolution ready")
    elif needs_human:
        lines.append("Outcome: needs human review — the agents handed this ticket to an engineer")
    else:
        lines.append(f"Outcome: {ticket.status.value.replace('_', ' ')}")

    if triage:
        bits = []
        if triage.get("priority"):
            bits.append(f"priority {triage['priority']}")
        if triage.get("category"):
            bits.append(f"category {triage['category']}")
        conf = _pct(triage.get("confidence"))
        lines.append("Triage: " + ", ".join(bits) + (f" (confidence {conf})" if conf else ""))

    root_cause = diagnosis.get("root_cause") or triage.get("root_cause_hypothesis")
    if root_cause:
        lines += ["", f"Likely root cause: {root_cause}"]

    steps = diagnosis.get("resolution_steps") or []
    if steps:
        lines += ["", "Suggested steps:"]
        for i, step in enumerate(steps, 1):
            if isinstance(step, dict):
                lines.append(f"{step.get('step', i)}. {step.get('action', '')}".rstrip())
                if step.get("command"):
                    lines.append(f"   Command: {step['command']}")
            else:
                lines.append(f"{i}. {step}")
    elif ticket.resolution_notes:
        lines += ["", ticket.resolution_notes.strip()]

    verify = diagnosis.get("verification_steps") or []
    if verify:
        lines += ["", "To verify:"] + [f"- {v}" for v in verify]

    lines += [
        "",
        f"Open in NEXUS: {settings.public_app_url.rstrip('/')}/tickets/{ticket.id}",
        "Written by an AI assistant. Review before acting on it.",
    ]
    return "\n".join(lines)[:8000]


async def write_back_ticket(ticket_id: uuid.UUID | str, force: bool = False) -> dict[str, Any]:
    """
    Post the agents' result to the source system. Safe to call repeatedly:
    the same note is never posted twice unless force=True.
    """
    ticket_id = uuid.UUID(str(ticket_id))
    async with get_db_context() as db:
        ticket = (await db.execute(
            select(Ticket).options(selectinload(Ticket.external_link).selectinload(ExternalTicketLink.connection))
            .where(Ticket.id == ticket_id)
        )).scalar_one_or_none()
        if ticket is None:
            return {"status": "not_found"}
        link = ticket.external_link
        if link is None:
            return {"status": "not_linked"}
        connection = link.connection
        if not (connection.write_back_notes or connection.write_back_status):
            return {"status": "disabled"}

        label = link.external_number or link.external_id
        note = build_note(ticket, label)
        if note is None:
            return {"status": "nothing_to_post"}
        note_hash = hashlib.sha256(note.encode()).hexdigest()
        if note_hash == link.last_note_hash and not force:
            return {"status": "already_posted"}

        connector = _connector_for(connection)
        actions: list[str] = []
        try:
            if connection.write_back_notes:
                await connector.post_note(link.external_id, note)
                actions.append("note")
            if (connection.write_back_status and connector.supports_status_update
                    and ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
                    and link.external_status not in ("resolved", "closed")):
                await connector.update_status(link.external_id, ticket.status.value,
                                              note=f"Resolved with NEXUS AI ({ticket.ticket_number}).")
                link.external_status = ticket.status.value
                actions.append("status")
        except ConnectorError as e:
            link.last_write_back_error = str(e)[:1000]
            logger.warning(f"Write-back to {connection.name} for {label} failed: {e}")
            return {"status": "failed", "error": str(e)}

        link.last_note_hash = note_hash
        link.last_written_back_at = _now()
        link.last_write_back_error = None
        db.add(AuditLog(
            user_id=None, action="integration.write_back", resource_type="ticket",
            resource_id=str(ticket.id), details={"connection": connection.name, "external": label, "actions": actions},
        ))
        return {"status": "posted", "actions": actions, "external": label}
