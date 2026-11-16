"""
NEXUS AI · rag/retrieval/vector_store.py
Qdrant vector store wrapper.

Responsibilities:
  - Create / ensure collection exists with correct schema
  - Upsert embeddings with rich payload (metadata)
  - Dense ANN search with optional payload filters (multi-tenant)
  - Delete by document ID or filter
  - Collection stats / health
"""

import asyncio
import uuid
from typing import Any

from langchain_core.documents import Document
from loguru import logger
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from rag.config import rag_settings


# ─────────────────────────────────────────────────────────────
#  Async client, one per event loop
# ─────────────────────────────────────────────────────────────
# The async client's connections belong to the event loop that opened them.
# The API runs on one long-lived loop, but each Celery task runs on a fresh
# loop that is closed afterwards (see run_async). Reusing one global client
# made every RAG search after a worker's first task fail with
# "Event loop is closed", so a new client is created whenever the loop changes.

_client: AsyncQdrantClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def get_qdrant_client() -> AsyncQdrantClient:
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is None or _client_loop is not loop:
        _client = AsyncQdrantClient(
            url=rag_settings.qdrant_url,
            api_key=rag_settings.qdrant_api_key or None,
            timeout=30,
        )
        _client_loop = loop
    return _client


# ─────────────────────────────────────────────────────────────
#  Collection management
# ─────────────────────────────────────────────────────────────

async def ensure_collection(collection_name: str | None = None) -> str:
    """
    Create the Qdrant collection if it doesn't exist.
    Idempotent — safe to call on every app startup.
    """
    name = collection_name or rag_settings.qdrant_collection
    client = get_qdrant_client()

    existing = await client.get_collections()
    existing_names = [c.name for c in existing.collections]

    if name not in existing_names:
        await client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(
                size=rag_settings.vector_dim,
                distance=Distance.COSINE,    # cosine sim for semantic search
            ),
        )
        # Create payload indexes for fast filtering
        for field in ["source_type", "category", "tenant_id", "ticket_number"]:
            await client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        logger.info(f"Created Qdrant collection: {name} (dim={rag_settings.vector_dim})")
    else:
        logger.debug(f"Qdrant collection exists: {name}")

    return name


async def get_collection_stats(collection_name: str | None = None) -> dict:
    """Return vector count and collection info."""
    name = collection_name or rag_settings.qdrant_collection
    client = get_qdrant_client()
    info = await client.get_collection(name)
    return {
        "name": name,
        "vector_count": info.points_count,
        "status": info.status,
        "config": {
            "size": info.config.params.vectors.size,
            "distance": info.config.params.vectors.distance.name,
        },
    }


# ─────────────────────────────────────────────────────────────
#  Upsert
# ─────────────────────────────────────────────────────────────

async def upsert_chunks(
    chunks: list[Document],
    vectors: list[list[float]],
    collection_name: str | None = None,
    tenant_id: str = "default",
) -> int:
    """
    Upsert chunk Documents + their embeddings into Qdrant.
    Uses chunk_id from metadata as the point ID (deterministic → idempotent).
    Returns the number of points upserted.
    """
    name = await ensure_collection(collection_name)
    client = get_qdrant_client()

    points: list[PointStruct] = []
    for chunk, vector in zip(chunks, vectors):
        # Use deterministic chunk_id or generate a random UUID
        point_id = chunk.metadata.get("chunk_id") or str(uuid.uuid4())
        # Qdrant point IDs must be UUID or unsigned int
        try:
            point_id = str(uuid.UUID(point_id))
        except ValueError:
            point_id = str(uuid.uuid4())

        payload = {
            "text": chunk.page_content,        # store content for retrieval
            "tenant_id": tenant_id,
            **{k: v for k, v in chunk.metadata.items()
               if isinstance(v, (str, int, float, bool, list)) and v is not None},
        }

        points.append(PointStruct(id=point_id, vector=vector, payload=payload))

    # Batch upsert in chunks of 100
    batch_size = 100
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        await client.upsert(collection_name=name, points=batch, wait=True)

    logger.info(f"Upserted {len(points)} vectors into '{name}' (tenant={tenant_id})")
    return len(points)


# ─────────────────────────────────────────────────────────────
#  Dense vector search
# ─────────────────────────────────────────────────────────────

async def dense_search(
    query_vector: list[float],
    top_k: int | None = None,
    collection_name: str | None = None,
    tenant_id: str | None = None,
    source_type_filter: str | None = None,
    category_filter: str | None = None,
) -> list[tuple[Document, float]]:
    """
    ANN search in Qdrant.
    Returns (Document, score) pairs sorted by cosine similarity descending.

    Supports multi-tenant filtering via tenant_id payload field.
    """
    name = collection_name or rag_settings.qdrant_collection
    k = top_k or rag_settings.dense_top_k
    client = get_qdrant_client()

    # Build optional payload filter
    conditions = []
    if tenant_id:
        conditions.append(FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)))
    if source_type_filter:
        conditions.append(FieldCondition(key="source_type", match=MatchValue(value=source_type_filter)))
    if category_filter:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category_filter)))

    query_filter = Filter(must=conditions) if conditions else None

    results = await client.search(
        collection_name=name,
        query_vector=query_vector,
        limit=k,
        query_filter=query_filter,
        with_payload=True,
    )

    docs_with_scores: list[tuple[Document, float]] = []
    for hit in results:
        payload = dict(hit.payload or {})
        text = payload.pop("text", "")
        doc = Document(page_content=text, metadata=payload)
        docs_with_scores.append((doc, hit.score))

    return docs_with_scores


async def delete_by_filter(
    collection_name: str | None = None,
    tenant_id: str | None = None,
    source_type: str | None = None,
) -> int:
    """Delete points matching a filter. Used for re-indexing or tenant cleanup."""
    name = collection_name or rag_settings.qdrant_collection
    client = get_qdrant_client()

    conditions = []
    if tenant_id:
        conditions.append(FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)))
    if source_type:
        conditions.append(FieldCondition(key="source_type", match=MatchValue(value=source_type)))

    if not conditions:
        raise ValueError("Provide at least one filter for deletion")

    result = await client.delete(
        collection_name=name,
        points_selector=Filter(must=conditions),
        wait=True,
    )
    logger.info(f"Deleted points from '{name}' matching filter")
    return result.status
