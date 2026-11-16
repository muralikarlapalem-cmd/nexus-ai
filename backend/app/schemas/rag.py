"""
NEXUS AI · app/schemas/rag.py
Pydantic v2 schemas for the RAG API endpoints.
"""

from typing import Any
from pydantic import BaseModel, Field


# ── Ingest ────────────────────────────────────────────────────

class IngestTextRequest(BaseModel):
    """Ingest raw text directly (for quick testing / agent writes)."""
    content: str = Field(..., min_length=10)
    title: str = Field(..., min_length=2, max_length=200)
    source_type: str = "document"   # "runbook" | "policy" | "document"
    category: str = "other"
    tenant_id: str = "default"
    metadata: dict[str, Any] = {}


class IngestResponse(BaseModel):
    status: str
    documents_in: int = 0
    chunks_created: int = 0
    vectors_upserted: int = 0
    bm25_doc_count: int = 0
    tenant_id: str = "default"
    reason: str | None = None       # populated on error/skip


# ── Search ────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    """Hybrid search — returns ranked documents without generation."""
    query: str = Field(..., min_length=3, max_length=1000)
    top_k: int = Field(5, ge=1, le=20)
    tenant_id: str = "default"
    source_type_filter: str | None = None
    category_filter: str | None = None


class SearchResult(BaseModel):
    source: str
    source_type: str                # "web" for web pages
    score: float | None = None      # None for web pages
    excerpt: str
    url: str | None = None          # set for web pages


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    total: int


# ── RAG Query ─────────────────────────────────────────────────

class RAGQueryRequest(BaseModel):
    """Full RAG: retrieve + generate answer."""
    query: str = Field(..., min_length=3, max_length=1000)
    tenant_id: str = "default"
    source_type_filter: str | None = None
    category_filter: str | None = None
    return_sources: bool = True


class RAGQueryResponse(BaseModel):
    answer: str
    sources: list[SearchResult]
    basis: str = "knowledge_base"   # knowledge_base | web | general_knowledge
    query: str
    docs_retrieved: int


# ── Collection stats ──────────────────────────────────────────

class CollectionStatsResponse(BaseModel):
    name: str
    vector_count: int | None = None
    status: str
    config: dict[str, Any]
    bm25_doc_count: int
