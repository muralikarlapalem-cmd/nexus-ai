"""
NEXUS AI · rag/retrieval/bm25_store.py
BM25 sparse retriever — keyword-based complement to dense vector search.

Why BM25 alongside vectors?
  Dense search excels at semantic similarity.
  BM25 excels at exact keyword matches (ticket numbers, error codes, CVE IDs).
  Hybrid fusion of both consistently outperforms either alone.

Storage: BM25 index is serialised to disk (pickle) under /rag/data, shared by the
         API and the Celery worker, and reloaded when the file changes.
Update:  Rebuilt whenever new documents are ingested (via Celery task).
"""

import os
import pickle
from pathlib import Path

from langchain_core.documents import Document
from loguru import logger
from rank_bm25 import BM25Okapi


# ─────────────────────────────────────────────────────────────
#  BM25Store
# ─────────────────────────────────────────────────────────────

class BM25Store:
    """
    In-process BM25 index backed by rank_bm25.BM25Okapi.
    Serialised to `index_path` so it survives restarts without re-embedding.
    """

    def __init__(self, index_path: str | None = None):
        # /rag is mounted into both the API and the Celery worker containers,
        # so both processes read and write the same file, and it survives
        # container re-creation (unlike /tmp inside a container).
        self.index_path = Path(index_path or os.getenv("BM25_INDEX_PATH", "/rag/data/nexus_bm25.pkl"))
        self._index: BM25Okapi | None = None
        self._documents: list[Document] = []
        self._loaded_mtime: float = 0.0   # file timestamp of the index we have in memory

    # ── Build / load ──────────────────────────────────────────

    def build(self, documents: list[Document]) -> None:
        """Build the BM25 index from a list of Documents."""
        self._documents = documents
        tokenised = [self._tokenise(doc.page_content) for doc in documents]
        self._index = BM25Okapi(tokenised)
        logger.info(f"BM25 index built: {len(documents)} documents")
        self._save()

    def add_documents(self, new_docs: list[Document]) -> None:
        """
        Incrementally add documents, deduplicating by chunk_id so re-ingesting
        the same source content overwrites existing entries instead of duplicating them.
        """
        merged: dict[str, Document] = {}
        for i, doc in enumerate(self._documents):
            key = doc.metadata.get("chunk_id") or f"__no_id_{i}"
            merged[key] = doc
        for i, doc in enumerate(new_docs):
            key = doc.metadata.get("chunk_id") or f"__no_id_new_{i}"
            merged[key] = doc
        self.build(list(merged.values()))

    def _save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file first, then swap it in, so another process
        # never reads a half-written index.
        tmp = self.index_path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump({"index": self._index, "documents": self._documents}, f)
        os.replace(tmp, self.index_path)
        self._loaded_mtime = self.index_path.stat().st_mtime
        logger.debug(f"BM25 index saved → {self.index_path}")

    def load(self) -> bool:
        """Load a previously saved index. Returns True if successful."""
        if not self.index_path.exists():
            logger.info("No BM25 index on disk — will build on first ingest")
            return False
        try:
            with open(self.index_path, "rb") as f:
                data = pickle.load(f)
            self._index = data["index"]
            self._documents = data["documents"]
            self._loaded_mtime = self.index_path.stat().st_mtime
            logger.info(f"BM25 index loaded: {len(self._documents)} documents from {self.index_path}")
            return True
        except Exception as e:
            logger.warning(f"Could not load BM25 index: {e}")
            return False

    def reload_if_changed(self) -> None:
        """Pick up an index written by another process (API vs Celery worker)."""
        try:
            mtime = self.index_path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._loaded_mtime:
            self.load()

    # ── Search ────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        tenant_id: str | None = None,
    ) -> list[tuple[Document, float]]:
        """
        BM25 keyword search.
        Returns (Document, normalised_score) pairs sorted by score descending.
        Scores are normalised to [0, 1] range for fair RRF fusion.
        """
        if self._index is None or not self._documents:
            logger.warning("BM25 index is empty — returning no results")
            return []

        tokens = self._tokenise(query)
        raw_scores = self._index.get_scores(tokens)

        # Build (doc, score) pairs with optional tenant filter
        candidates: list[tuple[Document, float]] = []
        for doc, score in zip(self._documents, raw_scores):
            if tenant_id and doc.metadata.get("tenant_id") != tenant_id:
                continue
            candidates.append((doc, float(score)))

        # Sort descending, take top_k
        candidates.sort(key=lambda x: x[1], reverse=True)
        candidates = candidates[:top_k]

        # Normalise scores to [0, 1]
        if candidates:
            max_score = candidates[0][1]
            if max_score > 0:
                candidates = [(doc, score / max_score) for doc, score in candidates]

        return candidates

    # ── Utilities ─────────────────────────────────────────────

    @staticmethod
    def _tokenise(text: str) -> list[str]:
        """Simple whitespace + lowercase tokeniser. Extend with stemming if needed."""
        return text.lower().split()

    @property
    def doc_count(self) -> int:
        return len(self._documents)

    @property
    def is_ready(self) -> bool:
        return self._index is not None and len(self._documents) > 0


# ── Singleton ─────────────────────────────────────────────────
_bm25_store: BM25Store | None = None


def get_bm25_store() -> BM25Store:
    """Return the process-level BM25 store, reloading if the file on disk changed."""
    global _bm25_store
    if _bm25_store is None:
        _bm25_store = BM25Store()
        _bm25_store.load()
    else:
        _bm25_store.reload_if_changed()
    return _bm25_store
