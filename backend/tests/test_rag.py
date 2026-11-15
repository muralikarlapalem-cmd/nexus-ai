"""
NEXUS AI · tests/test_rag.py
Unit + integration tests for the RAG pipeline.
Mocks Qdrant and Ollama so tests run without infra.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.documents import Document


# ─────────────────────────────────────────────────────────────
#  Chunker tests (no external deps)
# ─────────────────────────────────────────────────────────────

def test_chunk_documents_basic():
    from rag.ingestion.chunker import chunk_documents

    docs = [
        Document(
            page_content="This is a test document. " * 50,
            metadata={"source_type": "runbook", "file_name": "test.md", "file_path": "/test.md"},
        )
    ]
    chunks = chunk_documents(docs)
    assert len(chunks) > 0
    for chunk in chunks:
        assert "chunk_id" in chunk.metadata
        assert "chunk_index" in chunk.metadata
        assert len(chunk.page_content) > 0


def test_chunk_tickets_not_split():
    """Resolved tickets should not be re-chunked."""
    from rag.ingestion.chunker import chunk_documents

    ticket_doc = Document(
        page_content="TICKET: NX-2025-00001\nTITLE: Server down\nRESOLUTION: Restarted service.",
        metadata={
            "source_type": "resolved_ticket",
            "ticket_id": "abc-123",
            "ticket_number": "NX-2025-00001",
        },
    )
    chunks = chunk_documents([ticket_doc])
    assert len(chunks) == 1
    assert chunks[0].metadata["chunk_index"] == 0


def test_chunk_id_is_deterministic():
    from rag.ingestion.chunker import chunk_documents

    doc = Document(
        # Must be at least min_chunk_size (50) characters, or the chunker drops it
        page_content=(
            "Consistent content for the determinism test. Restart the nginx service "
            "and confirm the health endpoint returns 200."
        ),
        metadata={"source_type": "document", "file_name": "same.md", "file_path": "/same.md"},
    )
    chunks1 = chunk_documents([doc])
    chunks2 = chunk_documents([doc])
    assert chunks1[0].metadata["chunk_id"] == chunks2[0].metadata["chunk_id"]


# ─────────────────────────────────────────────────────────────
#  Loader tests
# ─────────────────────────────────────────────────────────────

def test_load_ticket_as_document():
    from rag.ingestion.loaders import load_ticket_as_document

    ticket = {
        "id": "abc-123",
        "ticket_number": "NX-2025-00001",
        "title": "Database connection timeout",
        "description": "App cannot connect to PostgreSQL",
        "category": "database",
        "priority": "high",
        "resolution_notes": "Increased max_connections from 100 to 200",
        "triage_output": {"root_cause": "connection pool exhausted"},
        "resolved_at": "2025-09-01",
        "tenant_id": "default",
    }
    doc = load_ticket_as_document(ticket)
    assert "NX-2025-00001" in doc.page_content
    assert "Database connection timeout" in doc.page_content
    assert "Increased max_connections" in doc.page_content
    assert doc.metadata["source_type"] == "resolved_ticket"
    assert doc.metadata["ticket_number"] == "NX-2025-00001"


# ─────────────────────────────────────────────────────────────
#  BM25 tests
# ─────────────────────────────────────────────────────────────

def test_bm25_search_basic():
    from rag.retrieval.bm25_store import BM25Store

    store = BM25Store(index_path="/tmp/test_bm25.pkl")
    docs = [
        Document(page_content="PostgreSQL connection pool exhausted max connections", metadata={"source_type": "ticket"}),
        Document(page_content="Network firewall blocking SSH traffic port 22", metadata={"source_type": "runbook"}),
        Document(page_content="Disk space full WAL logs PostgreSQL database", metadata={"source_type": "ticket"}),
    ]
    store.build(docs)

    results = store.search("PostgreSQL connection", top_k=2)
    assert len(results) > 0
    # PostgreSQL docs should rank above network doc
    top_doc, top_score = results[0]
    assert "PostgreSQL" in top_doc.page_content or "connection" in top_doc.page_content


def test_bm25_tenant_filter():
    from rag.retrieval.bm25_store import BM25Store

    store = BM25Store(index_path="/tmp/test_bm25_tenant.pkl")
    docs = [
        Document(page_content="network issue tenant A", metadata={"tenant_id": "tenant_a"}),
        Document(page_content="network issue tenant B", metadata={"tenant_id": "tenant_b"}),
    ]
    store.build(docs)

    results_a = store.search("network", top_k=5, tenant_id="tenant_a")
    assert all(doc.metadata["tenant_id"] == "tenant_a" for doc, _ in results_a)


# ─────────────────────────────────────────────────────────────
#  RRF fusion tests
# ─────────────────────────────────────────────────────────────

def test_rrf_fusion_deduplication():
    from rag.retrieval.hybrid_search import reciprocal_rank_fusion

    doc_a = Document(page_content="doc A", metadata={"chunk_id": "id-a"})
    doc_b = Document(page_content="doc B", metadata={"chunk_id": "id-b"})
    doc_c = Document(page_content="doc C", metadata={"chunk_id": "id-c"})

    dense  = [(doc_a, 0.9), (doc_b, 0.7), (doc_c, 0.5)]
    sparse = [(doc_b, 0.8), (doc_a, 0.6), (doc_c, 0.4)]

    fused = reciprocal_rank_fusion([dense, sparse])

    # Should deduplicate — 3 unique docs even though 6 items across 2 lists
    assert len(fused) == 3

    # doc_a and doc_b both appear in both lists — should rank above doc_c
    top_ids = [doc.metadata["chunk_id"] for doc, _ in fused[:2]]
    assert "id-a" in top_ids
    assert "id-b" in top_ids


def test_rrf_scores_descending():
    from rag.retrieval.hybrid_search import reciprocal_rank_fusion

    docs = [Document(page_content=f"doc {i}", metadata={"chunk_id": f"id-{i}"}) for i in range(5)]
    ranked = [(doc, 1.0 - i * 0.1) for i, doc in enumerate(docs)]

    fused = reciprocal_rank_fusion([ranked])
    scores = [score for _, score in fused]
    assert scores == sorted(scores, reverse=True)
