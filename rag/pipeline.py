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

import re
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
        timeout=rag_settings.llm_timeout_seconds,
        max_retries=rag_settings.llm_max_retries,
    )


# ─────────────────────────────────────────────────────────────
#  Prompts
# ─────────────────────────────────────────────────────────────

RAG_SYSTEM_PROMPT = """You are NEXUS AI, an expert IT Service Management assistant for an IT service desk.
You help with IT issues: errors and error codes, networks, databases, servers, cloud, security,
access problems, software and hardware, and the organisation's runbooks and past ticket resolutions.

Where your answer comes from, in order of preference:
1. INTERNAL KNOWLEDGE BASE (the organisation's runbooks and resolved tickets). When it is
   relevant, base the answer on it.
2. WEB RESULTS. Use them when the knowledge base has nothing relevant. Ignore results that
   are off-topic.
3. If neither has the answer, answer from your general IT knowledge. Say once, briefly, that
   the answer is not from the organisation's documents. Do not refuse a normal IT question
   just because the context is empty.

Citing: put the bracketed number of each item you use right after the sentence that uses it,
e.g. [1] for a knowledge-base item or [W2] for a web result. Cite only items you actually used;
never invent sources or URLs.

Other rules:
- Questions about IT products, technologies and concepts (e.g. "what is Oracle Database",
  "what is Kubernetes") are IT questions: answer them.
- If asked what you can do or what kind of questions to ask, explain your purpose and give a
  few example questions. Ignore the web results for such questions.
- Decline only requests that are clearly not about IT (poems, jokes, chit-chat), in one
  sentence, and say what you can help with.
- Answer the question directly. Don't introduce yourself or list what you can do unless asked.
- Be concise but complete. Use a numbered list for resolution steps.
- Highlight warnings, prerequisites, and any matching CVE or known issue.
"""

RAG_PROMPT = ChatPromptTemplate.from_messages([
    ("system", RAG_SYSTEM_PROMPT),
    ("human",
     "INTERNAL KNOWLEDGE BASE:\n{context}\n\n"
     "WEB RESULTS:\n{web}\n\n"
     "QUESTION: {question}\n\n"
     "(Cite every knowledge-base item or web result you use with its marker, "
     "e.g. [1] or [W1].)\n\nANSWER:"),
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


# Error codes / identifiers: ORA-01555, CVE-2024-3094, KB5034441, 0x80070005
_CODE_RE = re.compile(r"\b(?:[A-Za-z]{2,6}-\d{2,}(?:-\d+)?|KB\d{6,}|0x[0-9A-Fa-f]{6,})\b")


def _missing_codes(query: str, docs: list[tuple[Document, float]]) -> list[str]:
    """
    Error codes in the question that none of the matched documents mention.
    The reranker scores a runbook for ORA-01031 highly for a question about
    ORA-01555 (they look alike), which used to stop the web fallback.
    """
    text = " ".join(d.page_content for d, _ in docs).lower()
    return [c for c in dict.fromkeys(_CODE_RE.findall(query)) if c.lower() not in text]


WEB_CHECK_PROMPT = """Reply with only YES or NO.
Is the message below a technical IT question (about an error, product, technology,
configuration or troubleshooting) whose answer could be found by a web search?
Questions about this assistant itself (what it can do, what to ask), greetings,
and non-IT requests (poems, jokes, chit-chat) are NO.

Message: {question}"""


async def _needs_web_search(query: str) -> bool:
    """
    Quick check before the web fallback, so 'write a poem' or 'what can I ask'
    don't fetch unrelated pages (which also confused the answer). Only runs
    when the knowledge base had no answer. On any failure, search anyway.
    """
    try:
        llm = ChatOpenAI(model=rag_settings.llm_model, temperature=0, max_tokens=2,
                         api_key=rag_settings.openai_api_key, timeout=10)
        reply = await llm.ainvoke(WEB_CHECK_PROMPT.format(question=query[:1000]))
        return not reply.content.strip().upper().startswith("NO")
    except Exception as e:
        logger.warning(f"Web-search check failed, searching anyway: {e}")
        return True


async def _search_web(query: str):
    """Tavily web search (shared with the Diagnosis Agent). Returns None if unavailable."""
    try:
        from agents.tools.search_tools import search_web
    except ImportError as e:
        logger.warning(f"Web search not available: {e}")
        return None
    return await search_web(query)


def _format_web(web, reason: str) -> str:
    if web is None:
        return f"Not searched ({reason})."
    from agents.tools.search_tools import format_web_results
    return format_web_results(web)


async def _gather_context(
    query: str,
    tenant_id: str = "default",
    source_type_filter: str | None = None,
    category_filter: str | None = None,
    collection_name: str | None = None,
) -> dict[str, Any]:
    """
    Find what the answer should be based on:
      1. knowledge base (hybrid search + rerank), if it has a good match
      2. otherwise the web (Tavily)
      3. otherwise nothing: the LLM answers from general IT knowledge

    Weak knowledge-base matches are dropped rather than shown to the LLM.
    Before, every question was answered from the 3 closest documents however
    unrelated, so the LLM replied "the context does not contain..." to any
    question the runbooks don't cover.
    """
    candidates = await hybrid_search(
        query=query,
        tenant_id=tenant_id,
        source_type_filter=source_type_filter,
        category_filter=category_filter,
        collection_name=collection_name,
    )
    docs = await rerank_async(query=query, candidates=candidates)

    from rag.retrieval.reranker import _get_cross_encoder
    reranked = bool(docs) and rag_settings.reranker_enabled and _get_cross_encoder() is not None
    if reranked:
        # Cross-encoder scores: real matches > 0, unrelated documents ≈ -5 to -11
        docs = [(d, s) for d, s in docs if s >= rag_settings.kb_min_relevance]

    missing = _missing_codes(query, docs) if docs else []
    if missing:
        logger.info(f"Knowledge base matches don't mention {missing}; treating them as weak")
        docs = []

    web, web_reason = None, "the knowledge base had a relevant match"
    if not docs:
        if await _needs_web_search(query):
            web = await _search_web(query)
            if web is not None and not web.ok:
                logger.warning(f"Knowledge query web fallback failed: {web.error}")
        else:
            web_reason = "not a technical question"

    web_results = web.results if web is not None and web.ok else []

    # "ref" is the marker the LLM cites: [1].. for documents (_format_context
    # numbers them), [W1].. for web results (format_web_results numbers them)
    sources = [
        {
            "ref": f"[{i}]",
            "source": d.metadata.get("ticket_number") or d.metadata.get("file_name") or "Unknown",
            "source_type": d.metadata.get("source_type", ""),
            "score": round(s, 4),
            "excerpt": d.page_content[:300],
            "url": None,
        }
        for i, (d, s) in enumerate(docs, 1)
    ] + [
        {"ref": f"[W{i}]", "source": r["title"], "source_type": "web", "score": None,
         "excerpt": r["content"][:300], "url": r["url"]}
        for i, r in enumerate(web_results, 1)
    ]

    logger.info(
        f"Knowledge query '{query[:60]}': kb_matches={len(docs)} web_results={len(web_results)}"
    )
    return {
        "messages": RAG_PROMPT.format_messages(
            context=_format_context(docs) if docs else "No relevant internal documents.",
            web=_format_web(web, web_reason),
            question=query,
        ),
        "sources": sources,
        "docs_retrieved": len(docs),
    }


def cited_sources(answer: str, sources: list[dict]) -> tuple[list[dict], str]:
    """
    Keep only the sources the answer actually cites, without duplicates, and
    say what the answer was based on. Otherwise a declined or general answer
    ("write a poem", "what can I ask") listed unrelated web pages or runbooks
    as if it had used them.

    A source counts as cited when the answer has its marker ([1], [W2]) or,
    as a fallback, its URL or document name.
    """
    text = answer.lower()
    used, seen = [], set()
    for s in sources:
        key = s["url"] or s["source"]
        if key in seen:
            continue
        cited = (
            (s.get("ref") and s["ref"].lower() in text)
            or (s["url"] and s["url"].lower() in text)
            or (not s["url"] and s["source"].lower().removesuffix(".md") in text)
        )
        if cited:
            seen.add(key)
            used.append({k: v for k, v in s.items() if k != "ref"})

    if any(s["source_type"] != "web" for s in used):
        basis = "knowledge_base"
    elif used:
        basis = "web"
    else:
        basis = "general_knowledge"
    return used, basis


async def rag_query(
    query: str,
    tenant_id: str = "default",
    source_type_filter: str | None = None,
    category_filter: str | None = None,
    collection_name: str | None = None,
    return_sources: bool = True,
) -> dict[str, Any]:
    """
    Full RAG query: knowledge base → (web fallback) → LLM answer.

    Returns:
      {
        "answer": str,
        "sources": [{"source", "source_type", "score", "excerpt", "url"}],
        "basis": "knowledge_base" | "web" | "general_knowledge",
        "query": str,
        "docs_retrieved": int,
      }
    """
    ctx = await _gather_context(query, tenant_id, source_type_filter, category_filter, collection_name)
    response = await _get_llm().ainvoke(ctx["messages"])
    sources, basis = cited_sources(response.content, ctx["sources"])
    return {
        "answer": response.content,
        "sources": sources if return_sources else [],
        "basis": basis,
        "query": query,
        "docs_retrieved": ctx["docs_retrieved"],
    }


async def rag_query_stream(
    query: str,
    tenant_id: str = "default",
    **kwargs,
) -> AsyncIterator[dict[str, Any]]:
    """
    Streaming version of rag_query (for SSE). Yields, in order:
      {"token": str}                      for each piece of the answer
      {"sources": [...], "basis": str}   once, at the end (only cited sources)
    """
    ctx = await _gather_context(query, tenant_id=tenant_id, **kwargs)

    answer = ""
    async for chunk in _get_llm(streaming=True).astream(ctx["messages"]):
        if chunk.content:
            answer += chunk.content
            yield {"token": chunk.content}

    sources, basis = cited_sources(answer, ctx["sources"])
    yield {"sources": sources, "basis": basis}
