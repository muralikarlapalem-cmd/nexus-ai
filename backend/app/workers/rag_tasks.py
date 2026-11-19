"""
NEXUS AI · app/workers/rag_tasks.py
Celery tasks for RAG pipeline operations.

Tasks:
  ingest_resolved_tickets_task  — periodic: pull new resolved tickets into RAG
  ingest_document_task          — on-demand: ingest a single document path
  rebuild_bm25_task             — periodic: rebuild BM25 from Qdrant payload
"""

import asyncio
from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app, run_async

logger = get_task_logger(__name__)


@celery_app.task(
    name="app.workers.rag_tasks.ingest_resolved_tickets_task",
    queue="rag",
    max_retries=3,
    default_retry_delay=120,
)
def ingest_resolved_tickets_task(tenant_id: str = "default", limit: int = 200):
    """
    Periodic RAG task: fetch newly resolved tickets from Postgres
    and upsert them into Qdrant + BM25.

    Scheduled in celery_app.beat_schedule to run every 6 hours.
    """
    async def _run():
        from app.database import get_db_context
        from app.models.ticket import Ticket, TicketStatus
        from sqlalchemy import select

        try:
            from rag.ingestion.loaders import load_tickets_batch
            from rag.pipeline import ingest_documents
        except ImportError as e:
            logger.error(f"RAG module not available: {e}")
            return

        async with get_db_context() as db:
            result = await db.execute(
                select(Ticket)
                .where(Ticket.status.in_([TicketStatus.RESOLVED, TicketStatus.CLOSED]))
                .where(Ticket.resolution_notes.isnot(None))
                .where(Ticket.embedding_id.is_(None))   # only un-indexed tickets
                .limit(limit)
            )
            tickets = result.scalars().all()

            if not tickets:
                logger.info("No new resolved tickets to ingest")
                return

            ticket_dicts = [
                {
                    "id": str(t.id),
                    "ticket_number": t.ticket_number,
                    "title": t.title,
                    "description": t.description,
                    "category": t.category.value if t.category else "",
                    "priority": t.priority.value if t.priority else "",
                    "resolution_notes": t.resolution_notes,
                    "triage_output": t.triage_output,
                    "resolved_at": str(t.resolved_at),
                    "tenant_id": tenant_id,
                }
                for t in tickets
            ]

            docs = load_tickets_batch(ticket_dicts)
            from app.middleware.security import redact_documents
            redact_documents(docs)   # tickets created before PII redaction was switched on
            result_summary = await ingest_documents(docs, tenant_id=tenant_id)

            # Mark tickets as indexed
            for ticket in tickets:
                ticket.embedding_id = f"qdrant:{ticket.ticket_number}"
            await db.commit()

            logger.info(f"RAG task: ingested {len(tickets)} tickets → {result_summary}")

    run_async(_run())


@celery_app.task(
    name="app.workers.rag_tasks.ingest_document_task",
    queue="rag",
    max_retries=3,
)
def ingest_document_task(file_path: str, tenant_id: str = "default", source_type: str = "runbook"):
    """
    On-demand task: ingest a file from disk into the RAG knowledge base.
    Triggered when new runbooks or policy docs are deployed.
    """
    async def _run():
        from pathlib import Path
        try:
            from rag.ingestion.loaders import load_pdf, load_markdown, load_text
            from rag.pipeline import ingest_documents
        except ImportError as e:
            logger.error(f"RAG module not available: {e}")
            return

        path = Path(file_path)
        if not path.exists():
            logger.error(f"File not found: {file_path}")
            return

        metadata = {"source_type": source_type, "tenant_id": tenant_id}
        ext = path.suffix.lower()

        if ext == ".pdf":
            docs = load_pdf(path, metadata=metadata)
        elif ext in (".md", ".mdx"):
            docs = load_markdown(path, metadata=metadata)
        else:
            docs = load_text(path, metadata=metadata)

        from app.middleware.security import redact_documents
        redact_documents(docs)
        result = await ingest_documents(docs, tenant_id=tenant_id)
        logger.info(f"Ingested {file_path}: {result}")

    run_async(_run())


@celery_app.task(
    name="app.workers.rag_tasks.rebuild_bm25_task",
    queue="rag",
)
def rebuild_bm25_task():
    """
    Rebuild the BM25 index by scrolling all Qdrant payload text.
    Run this after a major re-index or on a weekly schedule.
    """
    async def _run():
        try:
            from rag.retrieval.vector_store import get_qdrant_client
            from rag.retrieval.bm25_store import get_bm25_store
            from rag.config import rag_settings
            from langchain_core.documents import Document
        except ImportError as e:
            logger.error(f"RAG module not available: {e}")
            return

        client = get_qdrant_client()
        bm25 = get_bm25_store()
        all_docs: list[Document] = []

        # Scroll through all Qdrant points
        offset = None
        while True:
            results, offset = await client.scroll(
                collection_name=rag_settings.qdrant_collection,
                limit=500,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in results:
                payload = dict(point.payload or {})
                text = payload.pop("text", "")
                if text:
                    all_docs.append(Document(page_content=text, metadata=payload))
            if offset is None:
                break

        if all_docs:
            bm25.build(all_docs)
            logger.info(f"BM25 rebuilt from Qdrant: {len(all_docs)} documents")
        else:
            logger.warning("Qdrant collection is empty — BM25 not rebuilt")

    run_async(_run())
