"""
NEXUS AI · agents/tools/rag_tool.py
RAG search tool — wraps the Phase 2 hybrid search pipeline as a LangChain tool.
Used by: Diagnosis Agent, Knowledge Agent, Security Agent.
"""

from dataclasses import dataclass, field

from langchain_core.documents import Document
from langchain_core.tools import tool
from loguru import logger

from rag.config import rag_settings


@dataclass
class KBSearchResult:
    """Outcome of a knowledge-base search. `error` is set when the search failed."""
    docs: list[tuple[Document, float]] = field(default_factory=list)
    reranked: bool = False          # True when scores come from the cross-encoder
    error: str | None = None

    @property
    def top_score(self) -> float | None:
        return self.docs[0][1] if self.docs else None

    @property
    def is_strong(self) -> bool:
        """
        True when the knowledge base has a good match. Without cross-encoder
        scores relevance can't be judged, so any result counts as a match.
        """
        if self.error or not self.docs:
            return False
        if not self.reranked:
            return True
        return self.top_score >= rag_settings.kb_min_relevance

    @property
    def sources(self) -> list[str]:
        return list(dict.fromkeys(source_name(d) for d, _ in self.docs))


def source_name(doc: Document) -> str:
    return doc.metadata.get("ticket_number") or doc.metadata.get("file_name") or "Unknown source"


async def search_knowledge_base(query: str, category_filter: str = "", top_k: int = 3) -> KBSearchResult:
    """Hybrid search + rerank over runbooks and resolved tickets. Never raises."""
    try:
        from rag.retrieval.hybrid_search import hybrid_search
        from rag.retrieval.reranker import _get_cross_encoder, rerank_async

        # Fetch the full candidate pool (rerank_candidates); the reranker narrows it to top_k
        results = await hybrid_search(query=query, category_filter=category_filter or None)
        reranked = await rerank_async(query=query, candidates=results, top_k=top_k)
        scored_by_reranker = bool(results) and rag_settings.reranker_enabled and _get_cross_encoder() is not None
        return KBSearchResult(docs=reranked, reranked=scored_by_reranker)
    except Exception as e:
        logger.warning(f"Knowledge base search failed: {e}")
        return KBSearchResult(error=f"{type(e).__name__}: {e}")


def format_kb_results(result: KBSearchResult) -> str:
    """Text for an LLM prompt; failures are stated plainly, never passed off as content."""
    if result.error:
        return ("KNOWLEDGE BASE UNAVAILABLE (the search failed). There are no internal "
                "results; do not treat this message as evidence about the ticket.")
    if not result.docs:
        return "No relevant documents found in the knowledge base."
    parts = [
        f"[Result {i}] Source: {source_name(doc)} (score: {score:.3f})\n{doc.page_content[:600]}"
        for i, (doc, score) in enumerate(result.docs, 1)
    ]
    text = "\n\n---\n\n".join(parts)
    if not result.is_strong:
        text = ("NOTE: these are the closest internal documents, but none is a good match "
                "for this ticket; rely on them only if clearly relevant.\n\n" + text)
    return text


@tool
async def rag_search(query: str, category_filter: str = "", top_k: int = 3) -> str:
    """
    Search the NEXUS AI knowledge base (runbooks + resolved tickets).
    Returns the most relevant documents for the query.

    Args:
        query: Natural-language search query
        category_filter: Optional — filter by 'network', 'database', 'security', etc.
        top_k: Number of results to return (1-5)
    """
    return format_kb_results(await search_knowledge_base(query, category_filter, top_k))


@tool
async def generate_runbook(ticket_summary: str, resolution_steps: str, category: str) -> str:
    """
    Generate a structured runbook from a resolved ticket.
    Used by the Knowledge Agent to auto-document resolutions.

    Args:
        ticket_summary: Brief description of the issue
        resolution_steps: Step-by-step resolution that worked
        category: Ticket category (network, database, etc.)
    """
    from langchain_openai import ChatOpenAI
    from rag.config import rag_settings

    llm = ChatOpenAI(
        model=rag_settings.llm_model,
        temperature=0.2,
        api_key=rag_settings.openai_api_key,
    )

    prompt = f"""Generate a concise, reusable runbook in Markdown format for the following resolved IT issue.
Include: Overview, Symptoms, Prerequisites, Step-by-step Resolution, and Verification.

Issue: {ticket_summary}
Category: {category}
Resolution applied: {resolution_steps}

Output ONLY the Markdown runbook, no preamble."""

    response = await llm.ainvoke(prompt)
    return response.content
