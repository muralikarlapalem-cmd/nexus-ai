"""
NEXUS AI · rag/retrieval/reranker.py
Cross-encoder reranker — precision step after hybrid search.

Why rerank?
  Hybrid search (dense + BM25) retrieves broad candidates (top 10–20).
  The cross-encoder reads query + each candidate together (full attention),
  producing much more accurate relevance scores at the cost of higher latency.
  We rerank the fused top-10 and return the top 3 for generation.

Model: cross-encoder/ms-marco-MiniLM-L-6-v2
  - Tiny (22M params) — runs on CPU in ~50ms for 10 candidates
  - Trained on MS MARCO passage ranking — generalises well to IT text
  - Swap for a larger model if recall matters more than latency
"""

import threading
from functools import lru_cache

from langchain_core.documents import Document
from loguru import logger

from rag.config import rag_settings

# Loading takes ~10s (torch + model). The lock stops the startup warm-up
# (rag/warmup.py) and a query that arrives meanwhile from loading it twice:
# the query waits for the warm-up's copy instead.
_load_lock = threading.Lock()


def _get_cross_encoder():
    """Return the cross-encoder, loading it on first use."""
    with _load_lock:
        return _load_cross_encoder()


@lru_cache(maxsize=1)
def _load_cross_encoder():
    """Load the cross-encoder model once and cache it."""
    try:
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(
            rag_settings.reranker_model,
            max_length=512,
            device="cpu",       # switch to "cuda" if GPU is available
        )
        logger.info(f"Reranker loaded: {rag_settings.reranker_model}")
        return model
    except ImportError:
        logger.warning("sentence-transformers not installed — reranker disabled")
        return None
    except Exception as e:
        logger.warning(f"Could not load reranker: {e} — reranker disabled")
        return None


def rerank(
    query: str,
    candidates: list[tuple[Document, float]],
    top_k: int | None = None,
) -> list[tuple[Document, float]]:
    """
    Rerank a list of (Document, score) candidates using a cross-encoder.

    Args:
        query:      The original user query
        candidates: [(Document, rrf_score)] from hybrid_search
        top_k:      How many to return (default: rag_settings.rerank_top_k)

    Returns:
        Reranked [(Document, cross_encoder_score)] — shorter list, higher precision
    """
    k = top_k or rag_settings.rerank_top_k

    if not rag_settings.reranker_enabled:
        return candidates[:k]

    if not candidates:
        return []

    cross_encoder = _get_cross_encoder()
    if cross_encoder is None:
        # Fallback: return top-k by hybrid score
        return candidates[:k]

    # Build (query, passage) pairs for the cross-encoder
    pairs = [(query, doc.page_content) for doc, _ in candidates]

    try:
        scores = cross_encoder.predict(pairs, show_progress_bar=False)
    except Exception as e:
        logger.warning(f"Reranker predict failed: {e} — using hybrid scores")
        return candidates[:k]

    # Zip scores back onto documents
    reranked = sorted(
        [(doc, float(score)) for (doc, _), score in zip(candidates, scores)],
        key=lambda x: x[1],
        reverse=True,
    )

    top = reranked[:k]
    logger.debug(
        f"Reranker: {len(candidates)} candidates → top {len(top)} "
        f"(scores: {[round(s, 3) for _, s in top]})"
    )
    return top


async def rerank_async(
    query: str,
    candidates: list[tuple[Document, float]],
    top_k: int | None = None,
) -> list[tuple[Document, float]]:
    """Async wrapper — runs cross-encoder in a threadpool."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, rerank, query, candidates, top_k)
