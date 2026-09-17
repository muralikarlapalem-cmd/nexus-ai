"""
NEXUS AI · rag/ingestion/embedder.py
Pluggable embedding layer.

Providers:
  OLLAMA  → nomic-embed-text (768-dim, free, local)
  OPENAI  → text-embedding-3-small (1536-dim, fast, cheap)

The provider is selected from rag_settings.embedding_provider.
Both return List[List[float]] and implement the same interface,
so the rest of the pipeline never cares which one is active.
"""

from functools import lru_cache
from typing import Protocol

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from loguru import logger

from rag.config import EmbeddingProvider, rag_settings


class EmbedderProtocol(Protocol):
    """Any class that can embed a list of strings."""
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


@lru_cache(maxsize=1)
def get_embedder() -> Embeddings:
    """
    Return a cached embedding model based on configured provider.
    Called once — reused across the whole RAG pipeline.
    """
    provider = rag_settings.embedding_provider

    if provider == EmbeddingProvider.OLLAMA:
        from langchain_ollama import OllamaEmbeddings
        embedder = OllamaEmbeddings(
            model=rag_settings.embedding_model_ollama,
            base_url=rag_settings.ollama_base_url,
        )
        logger.info(f"Embedder: Ollama / {rag_settings.embedding_model_ollama} (local, 768-dim)")
        return embedder

    elif provider == EmbeddingProvider.OPENAI:
        from langchain_openai import OpenAIEmbeddings
        embedder = OpenAIEmbeddings(
            model=rag_settings.embedding_model_openai,
            api_key=rag_settings.openai_api_key,
        )
        logger.info(f"Embedder: OpenAI / {rag_settings.embedding_model_openai} (1536-dim)")
        return embedder

    else:
        raise ValueError(f"Unsupported embedding provider: {provider}")


async def embed_documents_async(texts: list[str]) -> list[list[float]]:
    """
    Async wrapper for batch embedding.
    LangChain embedders are sync — run in threadpool for async contexts.
    """
    import asyncio
    embedder = get_embedder()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, embedder.embed_documents, texts)


async def embed_query_async(text: str) -> list[float]:
    """Async wrapper for single query embedding."""
    import asyncio
    embedder = get_embedder()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, embedder.embed_query, text)


def embed_chunks(chunks: list[Document]) -> tuple[list[Document], list[list[float]]]:
    """
    Embed a list of chunk Documents.
    Returns (chunks, vectors) in the same order.
    Logs embedding stats for monitoring.
    """
    embedder = get_embedder()
    texts = [c.page_content for c in chunks]

    logger.info(f"Embedding {len(texts)} chunks...")
    vectors = embedder.embed_documents(texts)

    logger.info(
        f"Embedding complete: {len(vectors)} vectors, "
        f"dim={len(vectors[0]) if vectors else 0}"
    )
    return chunks, vectors
