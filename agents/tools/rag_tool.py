"""
NEXUS AI · agents/tools/rag_tool.py
RAG search tool — wraps the Phase 2 hybrid search pipeline as a LangChain tool.
Used by: Diagnosis Agent, Knowledge Agent, Security Agent.
"""

import asyncio
from langchain_core.tools import tool


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
    try:
        from rag.retrieval.hybrid_search import hybrid_search
        from rag.retrieval.reranker import rerank_async

        results = await hybrid_search(
            query=query,
            top_k=max(1, min(top_k * 2, 10)),   # fetch more, rerank to top_k
            category_filter=category_filter or None,
        )
        reranked = await rerank_async(query=query, candidates=results, top_k=top_k)

        if not reranked:
            return "No relevant documents found in the knowledge base."

        parts = []
        for i, (doc, score) in enumerate(reranked, 1):
            source = (
                doc.metadata.get("ticket_number")
                or doc.metadata.get("file_name")
                or "Unknown source"
            )
            parts.append(
                f"[Result {i}] Source: {source} (score: {score:.3f})\n"
                f"{doc.page_content[:600]}"
            )
        return "\n\n---\n\n".join(parts)

    except Exception as e:
        return f"RAG search error: {str(e)}"


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
