"""
NEXUS AI · app/workers/integration_tasks.py
Celery tasks for external ticketing systems.

    sync_due_connections_task   every minute (Celery Beat): start syncs that are due
    sync_connection_task        sync one connection
    write_back_ticket_task      post the agents' result back to the source system
"""

from datetime import datetime, timedelta, timezone

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app, run_async

logger = get_task_logger(__name__)


@celery_app.task(name="app.workers.integration_tasks.sync_connection_task", queue="default")
def sync_connection_task(connection_id: str, trigger: str = "schedule", full: bool = False):
    from app.integrations.sync import sync_connection
    return run_async(sync_connection(connection_id, trigger=trigger, full=full))


@celery_app.task(name="app.workers.integration_tasks.sync_due_connections_task", queue="default")
def sync_due_connections_task():
    """Queue a sync for every enabled connection whose interval has passed since its last run."""
    async def _due() -> list[str]:
        from sqlalchemy import func, select

        from app.database import get_db_context
        from app.models.integration import IntegrationConnection, IntegrationSyncRun

        async with get_db_context() as db:
            last_run = (
                select(IntegrationSyncRun.connection_id, func.max(IntegrationSyncRun.started_at).label("last"))
                .group_by(IntegrationSyncRun.connection_id).subquery()
            )
            rows = (await db.execute(
                select(IntegrationConnection.id, IntegrationConnection.sync_interval_minutes, last_run.c.last)
                .outerjoin(last_run, last_run.c.connection_id == IntegrationConnection.id)
                .where(IntegrationConnection.enabled.is_(True))
            )).all()
        now = datetime.now(timezone.utc)
        return [str(cid) for cid, interval, last in rows
                if last is None or now - last >= timedelta(minutes=max(1, interval or 5))]

    due = run_async(_due())
    for connection_id in due:
        sync_connection_task.delay(connection_id, "schedule")
    if due:
        logger.info(f"Queued sync for {len(due)} connection(s)")
    return len(due)


@celery_app.task(
    name="app.workers.integration_tasks.write_back_ticket_task",
    queue="default", max_retries=3, default_retry_delay=60, bind=True,
)
def write_back_ticket_task(self, ticket_id: str):
    from app.integrations.sync import write_back_ticket
    result = run_async(write_back_ticket(ticket_id))
    if result.get("status") == "failed":
        # Source system briefly unreachable: try again later (up to 3 times)
        raise self.retry(exc=RuntimeError(result.get("error")))
    return result


def queue_write_back(ticket_id: str) -> None:
    """Called after an agent run. Never lets a write-back problem fail the agent task."""
    try:
        write_back_ticket_task.delay(ticket_id)
    except Exception as e:
        logger.warning(f"Could not queue write-back for {ticket_id}: {e}")
