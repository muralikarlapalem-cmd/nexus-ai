"""
NEXUS AI · app/api/v1/analytics.py
Service desk analytics, computed in SQL from the tickets table:
  GET /analytics/summary — tiles, daily trend, category mix and SLA by priority for a period

Definitions used throughout:
  period     the last `days` calendar days in the caller's time zone, today included
  resolved   a ticket with resolved_at set
  SLA        judged from timestamps, not the sla_breached flag (that flag is set
             by a 15-minute sweep of open tickets, so it misses tickets resolved
             late between sweeps):
               met       resolved at or before sla_due_at
               breached  resolved after sla_due_at, or still open past it
             Cancelled tickets and open tickets not yet due are not counted.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Date, and_, case, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_analyst_or_above
from app.database import get_async_db
from app.models.ticket import Ticket, TicketCategory, TicketPriority, TicketStatus
from app.models.user import User

router = APIRouter(prefix="/analytics", tags=["Analytics"])

OPEN_STATUSES = [
    TicketStatus.OPEN, TicketStatus.TRIAGED, TicketStatus.IN_PROGRESS, TicketStatus.AWAITING_USER,
]
PRIORITY_ORDER = [TicketPriority.CRITICAL, TicketPriority.HIGH, TicketPriority.MEDIUM, TicketPriority.LOW]


def _pct(met: int, breached: int) -> float | None:
    """Share of judged tickets that met their SLA; None when none were judged."""
    judged = met + breached
    return round(100 * met / judged, 1) if judged else None


@router.get("/summary")
async def get_summary(
    days: int = Query(30, ge=1, le=365, description="Length of the period in days, today included"),
    tz: str = Query("UTC", max_length=64, description="IANA time zone used to cut days, e.g. Asia/Kolkata"),
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_analyst_or_above),
):
    """
    Headline numbers and chart data for the Analytics page. The same numbers
    are returned for the period before, so the page can show the change.
    """
    # Postgres does the day arithmetic, so its time zone list is the one that counts
    known = await db.execute(text("SELECT 1 FROM pg_timezone_names WHERE name = :tz"), {"tz": tz})
    if known.first() is None:
        raise HTTPException(status_code=422, detail=f"Unknown time zone: {tz}")

    def local_day(column):
        return cast(func.timezone(tz, column), Date)

    today: date = (await db.execute(select(local_day(func.now())))).scalar_one()
    start = today - timedelta(days=days - 1)
    prev_start = start - timedelta(days=days)
    prev_end = start - timedelta(days=1)

    created_day = local_day(Ticket.created_at)
    resolved_day = local_day(Ticket.resolved_at)

    sla_met = and_(Ticket.resolved_at.is_not(None), Ticket.resolved_at <= Ticket.sla_due_at)
    sla_breached = or_(
        and_(Ticket.resolved_at.is_not(None), Ticket.resolved_at > Ticket.sla_due_at),
        and_(Ticket.resolved_at.is_(None), Ticket.status.in_(OPEN_STATUSES), Ticket.sla_due_at < func.now()),
    )
    met_count = func.count().filter(sla_met)
    breached_count = func.count().filter(sla_breached)
    resolve_seconds = func.extract("epoch", Ticket.resolved_at - Ticket.created_at)

    async def period_numbers(first: date, last: date) -> dict:
        created, met, breached = (await db.execute(
            select(func.count(), met_count, breached_count)
            .where(created_day.between(first, last))
        )).one()
        resolved, median_seconds = (await db.execute(
            select(func.count(), func.percentile_cont(0.5).within_group(resolve_seconds))
            .where(resolved_day.between(first, last))
        )).one()
        return {
            "created": created,
            "resolved": resolved,
            "sla_met": met,
            "sla_breached": breached,
            "sla_compliance_pct": _pct(met, breached),
            "median_resolve_hours": round(float(median_seconds) / 3600, 2) if median_seconds is not None else None,
        }

    current = await period_numbers(start, today)
    previous = await period_numbers(prev_start, prev_end)

    open_backlog, open_overdue = (await db.execute(
        select(func.count(), func.count().filter(Ticket.sla_due_at < func.now()))
        .where(Ticket.status.in_(OPEN_STATUSES))
    )).one()

    # Daily trend, with days that had no tickets filled in as zero
    created_by_day = dict((await db.execute(
        select(created_day, func.count()).where(created_day.between(start, today)).group_by(created_day)
    )).all())
    resolved_by_day = dict((await db.execute(
        select(resolved_day, func.count()).where(resolved_day.between(start, today)).group_by(resolved_day)
    )).all())
    trend = [
        {"date": d.isoformat(), "created": created_by_day.get(d, 0), "resolved": resolved_by_day.get(d, 0)}
        for d in (start + timedelta(days=i) for i in range(days))
    ]

    category_counts = dict((await db.execute(
        select(Ticket.category, func.count()).where(created_day.between(start, today)).group_by(Ticket.category)
    )).all())
    by_category = sorted(
        ({"category": c.value, "count": category_counts.get(c, 0)} for c in TicketCategory),
        key=lambda row: -row["count"],
    )

    sla_rows = {
        priority: (met, breached)
        for priority, met, breached in (await db.execute(
            select(Ticket.priority, met_count, breached_count)
            .where(created_day.between(start, today))
            .group_by(Ticket.priority)
        )).all()
    }
    sla_by_priority = []
    for p in PRIORITY_ORDER:
        met, breached = sla_rows.get(p, (0, 0))
        sla_by_priority.append({
            "priority": p.value, "met": met, "breached": breached, "compliance_pct": _pct(met, breached),
        })

    return {
        "period": {"days": days, "tz": tz, "start": start.isoformat(), "end": today.isoformat()},
        "previous_period": {"start": prev_start.isoformat(), "end": prev_end.isoformat()},
        "current": current,
        "previous": previous,
        "open_backlog": open_backlog,
        "open_overdue": open_overdue,
        "trend": trend,
        "by_category": by_category,
        "sla_by_priority": sla_by_priority,
    }
