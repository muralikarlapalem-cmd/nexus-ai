"""
NEXUS AI · rag/retrieval/hybrid_search.py
Hybrid search via Reciprocal Rank Fusion (RRF).

RRF combines ranked lists from multiple retrievers without needing
score normalisation across different scales.

Formula:  RRF(d) = Σ 1 / (k + rank_i(d))
  where k=60 (standard constant), rank_i is the position in retriever i's list.

Result: a single ranked list that benefits from both semantic (dense)
        and keyword (sparse/BM25) signals.
"""

from collections import defaultdict

from langchain_core.documents import Document
from loguru import logger

from rag.config import rag_settings
from rag.ingestion.embedder import embed_query_async
from rag.retrieval.bm25_store import get_bm25_store
from rag.retrieval.vector_store import dense_search


# ─────────────────────────────────────────────────────────────
#  RRF core
# ─────────────────────────────────────────────────────────────

def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[Document, float]]],
    k: int | None = None,
) -> list[tuple[Document, float]]:
    """
    Fuse multiple ranked lists using RRF.

    Args:
        ranked_lists: Each element is [(Document, score)] sorted desc by score.
        k: RRF constant (default: rag_settings.rrf_k = 60)

    Returns:
        Merged and re-ranked [(Document, rrf_score)] sorted desc.
    """
    rrf_k = k or rag_settings.rrf_k
    scores: dict[str, float] = defaultdict(float)
    doc_map: dict[str, Document] = {}

    for ranked_list in ranked_lists:
        for rank, (doc, _) in enumerate(ranked_list):
            # Use chunk_id or a hash of the content as the dedup key
            doc_id = (
                doc.metadata.get("chunk_id")
                or doc.metadata.get("ticket_id")
                or doc.page_content[:80]    # fallback: content prefix
            )
            scores[doc_id] += 1.0 / (rrf_k + rank + 1)
            doc_map[doc_id] = doc

    # Sort by RRF score descending
    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [(doc_map[doc_id], score) for doc_id, score in fused]


# ─────────────────────────────────────────────────────────────
#  Hybrid search entry point
# ─────────────────────────────────────────────────────────────

async def hybrid_search(
    query: str,
    top_k: int | None = None,
    tenant_id: str | None = None,
    source_type_filter: str | None = None,
    category_filter: str | None = None,
    collection_name: str | None = None,
) -> list[tuple[Document, float]]:
    """
    Full hybrid search:
      1. Dense: embed query → ANN search in Qdrant
      2. Sparse: BM25 keyword search
      3. Fuse: RRF
      4. Return top_k fused results

    Args:
        query:               Natural-language search query
        top_k:               Number of results to return after fusion
                             (default: rag_settings.rerank_candidates — the
                             pool the reranker then narrows to rerank_top_k)
        tenant_id:           Multi-tenant filter (None = all tenants)
        source_type_filter:  e.g. "runbook", "resolved_ticket"
        category_filter:     e.g. "network", "database"
        collection_name:     Override default Qdrant collection

    Returns:
        [(Document, rrf_score)] sorted by relevance descending
    """
    final_k = top_k or rag_settings.rerank_candidates
    dense_k = rag_settings.dense_top_k
    sparse_k = rag_settings.sparse_top_k

    # ── 1. Dense retrieval (async) ────────────────────────────
    query_vector = await embed_query_async(query)
    dense_results = await dense_search(
        query_vector=query_vector,
        top_k=dense_k,
        collection_name=collection_name,
        tenant_id=tenant_id,
        source_type_filter=source_type_filter,
        category_filter=category_filter,
    )
    logger.debug(f"Dense search: {len(dense_results)} results for '{query[:50]}'")

    # ── 2. Sparse retrieval (BM25) ────────────────────────────
    bm25 = get_bm25_store()
    sparse_results: list[tuple[Document, float]] = []
    if bm25.is_ready:
        sparse_results = bm25.search(query=query, top_k=sparse_k, tenant_id=tenant_id)
        logger.debug(f"BM25 search: {len(sparse_results)} results")
    else:
        logger.warning("BM25 store not ready — running dense-only search")

    # ── 3. RRF fusion ─────────────────────────────────────────
    fused = reciprocal_rank_fusion([dense_results, sparse_results])
    final = fused[:final_k]

    logger.info(
        f"Hybrid search: query='{query[:60]}' "
        f"dense={len(dense_results)} sparse={len(sparse_results)} "
        f"fused={len(fused)} → returning top {len(final)}"
    )
    return final


async def dense_only_search(
    query: str,
    top_k: int | None = None,
    tenant_id: str | None = None,
    **kwargs,
) -> list[tuple[Document, float]]:
    """Convenience: skip BM25, use dense vector search only."""
    query_vector = await embed_query_async(query)
    return await dense_search(
        query_vector=query_vector,
        top_k=top_k or rag_settings.dense_top_k,
        tenant_id=tenant_id,
        **kwargs,
    )
