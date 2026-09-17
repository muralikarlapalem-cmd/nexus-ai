"""
NEXUS AI · app/api/v1/rag.py
RAG API endpoints:
  POST /rag/ingest/text    — ingest raw text directly
  POST /rag/ingest/file    — upload + ingest a PDF or Markdown file
  POST /rag/ingest/tickets — ingest resolved tickets from Postgres
  POST /rag/search         — hybrid search (no generation)
  POST /rag/query          — full RAG: retrieve + generate
  GET  /rag/query/stream   — streaming RAG via SSE
  GET  /rag/stats          — collection + BM25 stats
  DELETE /rag/collection   — clear documents by filter (admin only)
"""

import json
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_agent_or_admin, require_admin
from app.database import get_async_db
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User
from app.schemas.rag import (
    CollectionStatsResponse,
    IngestResponse,
    IngestTextRequest,
    RAGQueryRequest,
    RAGQueryResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
)

# RAG imports (rag/ module mounted alongside backend/)
try:
    from rag.ingestion.loaders import load_tickets_batch, load_pdf, load_markdown
    from rag.pipeline import ingest_documents, rag_query, rag_query_stream
    from rag.retrieval.hybrid_search import hybrid_search
    from rag.retrieval.vector_store import get_collection_stats
    from rag.retrieval.bm25_store import get_bm25_store
    from langchain_core.documents import Document
    RAG_AVAILABLE = True
except ImportError as e:
    logger.warning(f"RAG module not available: {e}")
    RAG_AVAILABLE = False


router = APIRouter(prefix="/rag", tags=["RAG"])

ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".md", ".mdx", ".txt"}
MAX_UPLOAD_SIZE_MB = 50


def _check_rag_available():
    if not RAG_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG module not available. Install rag dependencies.",
        )


# ─────────────────────────────────────────────────────────────
#  INGEST ENDPOINTS
# ─────────────────────────────────────────────────────────────

@router.post("/ingest/text", response_model=IngestResponse)
async def ingest_text(
    body: IngestTextRequest,
    current_user: User = Depends(require_agent_or_admin),
):
    """
    Ingest raw text (e.g. a runbook written inline, a policy snippet).
    Useful for quick testing and agent-driven knowledge updates.
    """
    _check_rag_available()

    doc = Document(
        page_content=body.content,
        metadata={
            "source_type": body.source_type,
            "file_name": body.title,
            "category": body.category,
            "tenant_id": body.tenant_id,
            **body.metadata,
        },
    )

    result = await ingest_documents([doc], tenant_id=body.tenant_id)
    return IngestResponse(**result)


@router.post("/ingest/file", response_model=IngestResponse)
async def ingest_file(
    file: UploadFile = File(...),
    tenant_id: str = Form("default"),
    source_type: str = Form("runbook"),
    category: str = Form("other"),
    current_user: User = Depends(require_agent_or_admin),
):
    """
    Upload and ingest a PDF or Markdown file into the knowledge base.
    File is parsed, chunked, embedded, and upserted into Qdrant.
    """
    _check_rag_available()

    ext = Path(file.filename or "file").suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported file type: {ext}. Allowed: {ALLOWED_UPLOAD_EXTENSIONS}",
        )

    # Check size
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Max size: {MAX_UPLOAD_SIZE_MB}MB",
        )

    # Write to temp file for loaders
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        metadata = {"source_type": source_type, "category": category, "tenant_id": tenant_id}
        if ext == ".pdf":
            docs = load_pdf(tmp_path, metadata=metadata)
        else:
            docs = load_markdown(tmp_path, metadata=metadata)

        result = await ingest_documents(docs, tenant_id=tenant_id)
        logger.info(f"File ingested: {file.filename} by {current_user.username}")
        return IngestResponse(**result)
    finally:
        os.unlink(tmp_path)


@router.post("/ingest/tickets", response_model=IngestResponse)
async def ingest_resolved_tickets(
    tenant_id: str = "default",
    limit: int = 500,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_agent_or_admin),
):
    """
    Pull resolved tickets from Postgres and ingest them into the RAG knowledge base.
    This is the primary way historical resolutions become searchable.
    Typically triggered by a Celery periodic task (Phase 3+).
    """
    _check_rag_available()

    result = await db.execute(
        select(Ticket)
        .where(Ticket.status.in_([TicketStatus.RESOLVED, TicketStatus.CLOSED]))
        .where(Ticket.resolution_notes.isnot(None))
        .order_by(Ticket.resolved_at.desc())
        .limit(limit)
    )
    tickets = result.scalars().all()

    if not tickets:
        return IngestResponse(
            status="skipped",
            reason="No resolved tickets with resolution notes found",
            tenant_id=tenant_id,
        )

    # Convert ORM objects to dicts for the loader
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
    ingest_result = await ingest_documents(docs, tenant_id=tenant_id)
    logger.info(f"Ingested {len(tickets)} resolved tickets into RAG")
    return IngestResponse(**ingest_result)


# ─────────────────────────────────────────────────────────────
#  SEARCH & QUERY ENDPOINTS
# ─────────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchResponse)
async def search_knowledge_base(
    body: SearchRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Hybrid search (dense + BM25 + RRF) — returns ranked documents without LLM generation.
    Useful for agents to retrieve raw context before custom processing.
    """
    _check_rag_available()

    results = await hybrid_search(
        query=body.query,
        top_k=body.top_k,
        tenant_id=body.tenant_id,
        source_type_filter=body.source_type_filter,
        category_filter=body.category_filter,
    )

    return SearchResponse(
        query=body.query,
        results=[
            SearchResult(
                source=doc.metadata.get("ticket_number") or doc.metadata.get("file_name") or "Unknown",
                source_type=doc.metadata.get("source_type", ""),
                score=round(score, 4),
                excerpt=doc.page_content[:400],
            )
            for doc, score in results
        ],
        total=len(results),
    )


@router.post("/query", response_model=RAGQueryResponse)
async def query_knowledge_base(
    body: RAGQueryRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Full RAG query: retrieve relevant docs + generate answer with Claude.
    Returns the answer and the source documents used.
    """
    _check_rag_available()

    result = await rag_query(
        query=body.query,
        tenant_id=body.tenant_id,
        source_type_filter=body.source_type_filter,
        category_filter=body.category_filter,
        return_sources=body.return_sources,
    )

    return RAGQueryResponse(
        answer=result["answer"],
        sources=[SearchResult(**s) for s in result["sources"]],
        query=result["query"],
        docs_retrieved=result["docs_retrieved"],
    )


@router.get("/query/stream")
async def query_stream(
    query: str,
    tenant_id: str = "default",
    current_user: User = Depends(get_current_user),
):
    """
    Streaming RAG query via Server-Sent Events (SSE).
    The frontend can consume this with EventSource for real-time token display.
    """
    _check_rag_available()

    async def event_generator():
        try:
            async for token in rag_query_stream(query=query, tenant_id=tenant_id):
                yield f"data: {json.dumps({'token': token})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────
#  STATS & ADMIN
# ─────────────────────────────────────────────────────────────

@router.get("/stats", response_model=CollectionStatsResponse)
async def get_stats(current_user: User = Depends(require_agent_or_admin)):
    """Return Qdrant collection stats + BM25 document count."""
    _check_rag_available()
    stats = await get_collection_stats()
    bm25 = get_bm25_store()
    return CollectionStatsResponse(**stats, bm25_doc_count=bm25.doc_count)


@router.delete("/collection", status_code=status.HTTP_204_NO_CONTENT)
async def clear_collection(
    tenant_id: str | None = None,
    source_type: str | None = None,
    current_user: User = Depends(require_admin),
):
    """
    Delete vectors by filter (admin only).
    Requires at least one filter to prevent accidental full wipe.
    """
    _check_rag_available()
    from rag.retrieval.vector_store import delete_by_filter
    if not tenant_id and not source_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide tenant_id or source_type to delete",
        )
    await delete_by_filter(tenant_id=tenant_id, source_type=source_type)
