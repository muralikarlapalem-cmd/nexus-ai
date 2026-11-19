"""
NEXUS AI · rag/config.py
RAG pipeline configuration.
All values default from environment — override in .env.
"""

from enum import Enum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingProvider(str, Enum):
    OLLAMA = "ollama"          # local, free — nomic-embed-text (768-dim)
    OPENAI = "openai"          # text-embedding-3-small (1536-dim)


class RAGSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Qdrant ───────────────────────────────────────────────
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""              # empty = no auth (local dev)
    qdrant_collection: str = "nexus_knowledge"

    # ── Embeddings ────────────────────────────────────────────
    embedding_provider: EmbeddingProvider = EmbeddingProvider.OPENAI
    embedding_model_ollama: str = "nomic-embed-text"          # 768-dim (free local)
    embedding_model_openai: str = "text-embedding-3-small"    # 1536-dim (~$0.02/1M tokens)
    ollama_base_url: str = "http://ollama:11434"

    # Vector dimension — must match the embedding model chosen
    vector_dim: int = 1536     # 1536 for openai/text-embedding-3-small, 768 for nomic

    # ── Chunking ──────────────────────────────────────────────
    chunk_size: int = 512          # tokens per chunk
    chunk_overlap: int = 64        # overlap between consecutive chunks
    min_chunk_size: int = 50       # discard chunks smaller than this

    # ── Retrieval ─────────────────────────────────────────────
    dense_top_k: int = 10          # candidates from vector search
    sparse_top_k: int = 10         # candidates from BM25
    rerank_candidates: int = 20    # fused candidates handed to the reranker
    rerank_top_k: int = 3          # final docs after reranking
    rrf_k: int = 60                # RRF constant (higher = smoother fusion)

    # ── Reranker ──────────────────────────────────────────────
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_enabled: bool = True

    # ── Generation ────────────────────────────────────────────
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.0
    # Per-request limit for agent LLM calls. Without it a call can hang ~10 min
    # when the internet drops; with it the run fails fast and the worker retries.
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    max_tokens: int = 1024

    # ── Web search fallback (Diagnosis Agent) ─────────────────
    # The web is searched only when the knowledge base has no good match:
    # no results, a search error, or a best reranker score below kb_min_relevance.
    # ms-marco cross-encoder scores: real matches are > 0, unrelated docs ≈ -5 to -11.
    kb_min_relevance: float = 0.0
    web_search_max_results: int = 3
    web_search_timeout: float = 15.0

    # ── Keys (inherited from main .env) ──────────────────────
    openai_api_key: str = ""
    tavily_api_key: str = ""           # empty = web search disabled


@lru_cache
def get_rag_settings() -> RAGSettings:
    return RAGSettings()


rag_settings = get_rag_settings()
