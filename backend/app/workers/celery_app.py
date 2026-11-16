"""
NEXUS AI · app/workers/celery_app.py
Celery application — async task queue for agent dispatch and background jobs.

Phase 1: Task stubs defined (no agent logic yet).
Phase 3: process_ticket_task will invoke the LangGraph supervisor.
"""

import asyncio
from celery import Celery
from celery.signals import worker_process_init
from celery.utils.log import get_task_logger
from kombu import Queue

from app.config import settings

logger = get_task_logger(__name__)

# ── Celery app ────────────────────────────────────────────────
celery_app = Celery(
    "nexus_ai",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.rag_tasks"],
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Routing — dedicated queues per concern
    task_default_queue="default",
    task_queues=(
        Queue("default"),
        Queue("agents"),        # LangGraph agent tasks (Phase 3)
        Queue("rag"),           # Embedding + indexing tasks (Phase 2)
        Queue("evals"),         # Evaluation jobs (Phase 5)
        Queue("fine_tuning"),   # Training data prep (Phase 6)
    ),

    # Reliability
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,       # fair dispatch for long tasks

    # Result expiry
    result_expires=86400,               # 24h

    # Retry defaults
    task_max_retries=3,
    task_default_retry_delay=60,        # seconds

    # Beat schedule (periodic tasks)
    beat_schedule={
        "check-sla-breaches-every-15min": {
            "task": "app.workers.celery_app.check_sla_breaches",
            "schedule": 900.0,                  # every 15 minutes
        },
        # Phase 2: RAG ingestion — sync new resolved tickets every 6 hours
        "ingest-resolved-tickets-every-6h": {
            "task": "app.workers.rag_tasks.ingest_resolved_tickets_task",
            "schedule": 21600.0,                # every 6 hours
            "kwargs": {"tenant_id": "default", "limit": 200},
        },
        # Phase 2: Rebuild BM25 from Qdrant every Sunday at midnight
        "rebuild-bm25-weekly": {
            "task": "app.workers.rag_tasks.rebuild_bm25_task",
            "schedule": 604800.0,               # every 7 days
        },
    },
)


# ── Warm up the RAG models in each worker process ─────────────
@worker_process_init.connect
def _warm_up_rag_models(**_):
    """
    Start loading the reranker + embedder as soon as a worker process starts,
    so the first ticket's diagnosis doesn't wait for them. It runs in a
    background thread: Celery kills a child process whose init takes longer
    than a few seconds, and the load takes ~10s.
    """
    try:
        from rag.warmup import start_warm_up
        start_warm_up()
    except ImportError as e:
        logger.warning(f"RAG warm-up skipped: {e}")


# ── Helper: run async code inside a Celery task ───────────────
def run_async(coro):
    """
    Run an async coroutine from a sync Celery task on a fresh event loop.
    Disposes the shared async engine's connection pool afterward, on this
    same loop, so no asyncpg connections remain bound to a loop that's
    about to close (avoids "attached to a different loop" errors on the
    next task run).
    """
    from app.database import engine

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()


# ─────────────────────────────────────────────────────────────
#  PHASE 1 TASKS
# ─────────────────────────────────────────────────────────────

@celery_app.task(name="app.workers.celery_app.check_sla_breaches", queue="default")
def check_sla_breaches():
    """
    Periodic task: scan open tickets past their SLA deadline and flag them.
    Runs every 15 minutes via Celery Beat.
    """
    from app.database import get_db_context
    from app.models.ticket import Ticket, TicketStatus
    from sqlalchemy import select, update
    from datetime import datetime, timezone

    async def _run():
        async with get_db_context() as db:
            now = datetime.now(timezone.utc)
            result = await db.execute(
                select(Ticket).where(
                    Ticket.sla_due_at < now,
                    Ticket.sla_breached == False,  # noqa: E712
                    Ticket.status.in_([TicketStatus.OPEN, TicketStatus.TRIAGED, TicketStatus.IN_PROGRESS])
                )
            )
            breached_tickets = result.scalars().all()
            for ticket in breached_tickets:
                ticket.sla_breached = True
                logger.warning(f"SLA breached: {ticket.ticket_number}")
            if breached_tickets:
                await db.commit()
                logger.info(f"Flagged {len(breached_tickets)} SLA-breached tickets")

    run_async(_run())


# ─────────────────────────────────────────────────────────────
#  PHASE 3 TASK STUBS (agent invocation)
# ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.workers.celery_app.process_ticket_task",
    queue="agents",
    max_retries=3,
    default_retry_delay=30,
)
def process_ticket_task(ticket_id: str):
    """
    Dispatch a ticket through the LangGraph supervisor.
    STUB — full implementation in Phase 3.
    """
    logger.info(f"[STUB] process_ticket_task called for ticket_id={ticket_id}")
    from agents.supervisor import run_supervisor
    run_async(run_supervisor(ticket_id))


@celery_app.task(
    name="app.workers.celery_app.index_ticket_for_rag",
    queue="rag",
    max_retries=3,
)
def index_ticket_for_rag(ticket_id: str):
    """
    Embed a resolved ticket and upsert it into Qdrant.
    STUB — full implementation in Phase 2.
    """
    logger.info(f"[STUB] index_ticket_for_rag called for ticket_id={ticket_id}")


@celery_app.task(
    name="app.workers.celery_app.run_eval_suite",
    queue="evals",
)
def run_eval_suite(run_id: str):
    """
    Run RAGAS + DeepEval against a LangSmith run.
    STUB — full implementation in Phase 5.
    """
    logger.info(f"[STUB] run_eval_suite called for run_id={run_id}")
