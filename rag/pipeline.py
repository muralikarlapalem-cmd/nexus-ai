"""
NEXUS AI · rag/pipeline.py
End-to-end RAG pipeline built with LangChain LCEL.

Two public interfaces:

  ingest_documents(docs, tenant_id)
    → chunk → embed → upsert Qdrant + rebuild BM25

  rag_query(query, tenant_id, ...)
    → hybrid_search → rerank → format_context → LLM → answer + sources

The pipeline is designed to be called by:
  - FastAPI endpoints (async)
  - Celery tasks (via run_async)
  - LangGraph agents (Phase 3) — agents call rag_query as a tool
"""

from typing import Any, AsyncIterator

from langchain_openai import ChatOpenAI
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from loguru import logger

from rag.config import rag_settings
from rag.ingestion.chunker import chunk_documents
from rag.ingestion.embedder import embed_chunks
from rag.retrieval.bm25_store import get_bm25_store
from rag.retrieval.hybrid_search import hybrid_search
from rag.retrieval.reranker import rerank_async
from rag.retrieval.vector_store import ensure_collection, upsert_chunks


# ─────────────────────────────────────────────────────────────
#  LLM
# ─────────────────────────────────────────────────────────────

def _get_llm(streaming: bool = False) -> ChatOpenAI:
    return ChatOpenAI(
        model=rag_settings.llm_model,
        temperature=rag_settings.llm_temperature,
        max_tokens=rag_settings.max_tokens,
        api_key=rag_settings.openai_api_key,
        streaming=streaming,
    )


# ─────────────────────────────────────────────────────────────
#  Prompts
# ─────────────────────────────────────────────────────────────

RAG_SYSTEM_PROMPT = """You are NEXUS AI, an expert IT Service Management assistant.
Answer the user's question using ONLY the context provided below.
If the context does not contain enough information to answer, say so clearly.
Always cite the source document (ticket number, runbook name, or file) when referencing it.

Guidelines:
- Be concise but complete
- For resolution steps, use a numbered list
- Highlight any warnings or prerequisites
- If you see a matching CVE or known issue, surface it explicitly
"""

RAG_PROMPT = ChatPromptTemplate.from_messages([
    ("system", RAG_SYSTEM_PROMPT),
    ("human", "CONTEXT:\n{context}\n\nQUESTION: {question}\n\nANSWER:"),
])


# ─────────────────────────────────────────────────────────────
#  Ingestion pipeline
# ─────────────────────────────────────────────────────────────

async def ingest_documents(
    documents: list[Document],
    tenant_id: str = "default",
    collection_name: str | None = None,
) -> dict[str, Any]:
    """
    Full ingestion pipeline:
      Documents → chunk → embed → upsert Qdrant → rebuild BM25

    Returns a summary dict with counts.
    """
    if not documents:
        return {"status": "skipped", "reason": "no documents provided"}

    logger.info(f"Ingesting {len(documents)} documents (tenant={tenant_id})")

    # 1. Ensure collection exists
    await ensure_collection(collection_name)

    # 2. Chunk
    chunks = chunk_documents(documents)
    if not chunks:
        return {"status": "error", "reason": "chunking produced no chunks"}

    # 3. Embed
    chunks, vectors = embed_chunks(chunks)

    # 4. Upsert into Qdrant
    upserted = await upsert_chunks(
        chunks=chunks,
        vectors=vectors,
        collection_name=collection_name,
        tenant_id=tenant_id,
    )

    # 5. Rebuild BM25 (add new docs to existing index)
    bm25 = get_bm25_store()
    bm25.add_documents(chunks)

    result = {
        "status": "success",
        "documents_in": len(documents),
        "chunks_created": len(chunks),
        "vectors_upserted": upserted,
        "bm25_doc_count": bm25.doc_count,
        "tenant_id": tenant_id,
    }
    logger.info(f"Ingestion complete: {result}")
    return result


# ─────────────────────────────────────────────────────────────
#  RAG query pipeline
# ─────────────────────────────────────────────────────────────

def _format_context(docs_with_scores: list[tuple[Document, float]]) -> str:
    """Format retrieved documents into a clean context string for the LLM."""
    if not docs_with_scores:
        return "No relevant documents found."

    parts = []
    for i, (doc, score) in enumerate(docs_with_scores, 1):
        source = (
            doc.metadata.get("ticket_number")
            or doc.metadata.get("file_name")
            or doc.metadata.get("parent_source")
            or "Unknown source"
        )
        section = doc.metadata.get("h2") or doc.metadata.get("h1") or ""
        section_label = f" › {section}" if section else ""

        parts.append(
            f"[{i}] SOURCE: {source}{section_label} (relevance: {score:.3f})\n"
            f"{doc.page_content.strip()}"
        )
    return "\n\n---\n\n".join(parts)


async def rag_query(
    query: str,
    tenant_id: str = "default",
    source_type_filter: str | None = None,
    category_filter: str | None = None,
    collection_name: str | None = None,
    return_sources: bool = True,
) -> dict[str, Any]:
    """
    Full RAG query:
      1. Hybrid search (dense + BM25)
      2. Rerank
      3. Format context
      4. LLM generation

    Returns:
      {
        "answer": str,
        "sources": [{"source": str, "score": float, "excerpt": str}],
        "query": str,
        "docs_retrieved": int,
      }
    """
    # 1. Retrieve
    candidates = await hybrid_search(
        query=query,
        tenant_id=tenant_id,
        source_type_filter=source_type_filter,
        category_filter=category_filter,
        collection_name=collection_name,
    )

    # 2. Rerank
    final_docs = await rerank_async(query=query, candidates=candidates)

    # 3. Format context
    context = _format_context(final_docs)

    # 4. Generate
    llm = _get_llm()
    messages = RAG_PROMPT.format_messages(context=context, question=query)
    response = await llm.ainvoke(messages)
    answer = response.content

    # 5. Build source list
    sources = []
    if return_sources:
        for doc, score in final_docs:
            sources.append({
                "source": (
                    doc.metadata.get("ticket_number")
                    or doc.metadata.get("file_name")
                    or "Unknown"
                ),
                "source_type": doc.metadata.get("source_type", ""),
                "score": round(score, 4),
                "excerpt": doc.page_content[:300],
            })

    logger.info(f"RAG query answered: '{query[:60]}' → {len(final_docs)} docs used")
    return {
        "answer": answer,
        "sources": sources,
        "query": query,
        "docs_retrieved": len(final_docs),
    }


async def rag_query_stream(
    query: str,
    tenant_id: str = "default",
    **kwargs,
) -> AsyncIterator[str]:
    """
    Streaming version of rag_query.
    Yields answer tokens as they are generated (for SSE / WebSocket).
    """
    candidates = await hybrid_search(query=query, tenant_id=tenant_id, **kwargs)
    final_docs = await rerank_async(query=query, candidates=candidates)
    context = _format_context(final_docs)

    llm = _get_llm(streaming=True)
    messages = RAG_PROMPT.format_messages(context=context, question=query)

    async for chunk in llm.astream(messages):
        if chunk.content:
            yield chunk.content
