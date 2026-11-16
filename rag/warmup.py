"""
NEXUS AI · rag/warmup.py
Load the RAG models when a process starts, not on the first user query.

Without this, the first AI Knowledge Query (or ticket diagnosis) after a
restart waited ~10s while torch and the cross-encoder reranker loaded.
The warm-up runs in a background thread so the API and Celery workers
still start immediately; a query that arrives mid-warm-up simply waits
for the load already in progress (see the lock in reranker.py).
"""

import threading
import time

from loguru import logger

from rag.config import rag_settings


def warm_up_models() -> None:
    """Load the reranker and the embedding client. Never raises."""
    started = time.perf_counter()
    try:
        from rag.ingestion.embedder import get_embedder
        get_embedder()

        if rag_settings.reranker_enabled:
            from rag.retrieval.reranker import _get_cross_encoder
            model = _get_cross_encoder()
            if model is not None:
                # The first prediction is slower than the rest; do it now
                model.predict([("warm up", "warm up")], show_progress_bar=False)

        logger.info(f"RAG models warmed up in {time.perf_counter() - started:.1f}s")
    except Exception as e:
        logger.warning(f"RAG model warm-up failed (models will load on first use): {e}")


def start_warm_up() -> threading.Thread:
    """Run warm_up_models in a background thread and return the thread."""
    thread = threading.Thread(target=warm_up_models, name="rag-warmup", daemon=True)
    thread.start()
    return thread
