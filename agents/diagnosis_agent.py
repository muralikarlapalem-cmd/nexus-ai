"""
NEXUS AI · agents/diagnosis_agent.py
Diagnosis Agent — RAG-powered root cause analysis.

Uses: knowledge base search, web search (only when the knowledge base has no
good match), check_sla_status
Produces: structured diagnosis with step-by-step resolution plan, plus the
internal documents and web pages it was based on
"""

import json
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import TicketState
from agents.tools.rag_tool import format_kb_results, search_knowledge_base
from agents.tools.search_tools import WebSearchResult, format_web_results, search_web
from agents.tools.ticket_tools import check_sla_status
from rag.config import rag_settings


DIAGNOSIS_SYSTEM_PROMPT = """You are the Diagnosis Agent for NEXUS AI IT Service Management.
You are an expert IT engineer performing root cause analysis.

You will be given a ticket and context from the knowledge base.
Respond with ONLY valid JSON in this schema:
{
  "root_cause": "precise root cause statement",
  "confidence": 0.0-1.0,
  "evidence": ["list of evidence points from KB or web"],
  "resolution_steps": [
    {"step": 1, "action": "...", "command": "optional CLI/SQL command", "expected_outcome": "..."},
    ...
  ],
  "verification_steps": ["how to verify the fix worked"],
  "prevention": "how to prevent recurrence",
  "estimated_resolution_time": "e.g. 30 minutes",
  "escalation_needed": true/false,
  "escalation_reason": "string or null",
  "related_tickets": ["ticket numbers from KB if found"],
  "references": ["source names used"]
}

Be specific. Include actual commands where relevant.
Prefer the knowledge base results. Use web results ([W1], [W2], ...) only when they are relevant,
and list the web pages you relied on in "references" by their URL.
If neither source has relevant information, say so in "evidence", base the answer on general
IT knowledge, keep "confidence" at 0.5 or lower, and do not invent document names or URLs."""


def _sources_text(kb_sources: list[str], web_sources: list[dict]) -> str:
    """The 'Sources' block appended to the resolution notes shown on the ticket."""
    if not kb_sources and not web_sources:
        return ("Sources: none. No matching internal document or web page was found; "
                "this answer is based on the AI model's general IT knowledge. Verify before applying.")
    lines = ["Sources:"]
    lines += [f"- Knowledge base: {s}" for s in kb_sources]
    lines += [f"- Web: {w['title']} - {w['url']}" for w in web_sources]
    return "\n".join(lines)


async def diagnosis_agent(state: TicketState) -> TicketState:
    """LangGraph node: Diagnosis Agent."""
    llm = ChatOpenAI(
        model=rag_settings.llm_model,
        temperature=0.0,
        api_key=rag_settings.openai_api_key,
        timeout=rag_settings.llm_timeout_seconds,
        max_retries=rag_settings.llm_max_retries,
    )

    triage = state.get("triage_result", {})
    category = triage.get("category", state["category"])
    keywords = triage.get("keywords", [])

    # ── Tool calls ────────────────────────────────────────────
    # 1. Knowledge base search using triage keywords
    search_query = f"{state['title']} {' '.join(keywords[:3])} {category}"
    kb = await search_knowledge_base(search_query, category_filter=category, top_k=3)
    kb_context = format_kb_results(kb)

    # 2. Web search, only when the knowledge base has no good match
    web: WebSearchResult | None = None
    if kb.is_strong:
        web_context = "Not searched: the knowledge base has a good match for this ticket."
    else:
        web = await search_web(f"{state['title']} {' '.join(keywords[:3])} fix", max_results=3)
        web_context = format_web_results(web)
    web_sources = [{"title": r["title"], "url": r["url"]} for r in (web.results if web else [])]

    # 3. SLA check
    sla_status = await check_sla_status.ainvoke({"ticket_id": state["ticket_id"]})

    # ── LLM diagnosis ─────────────────────────────────────────
    user_prompt = f"""TICKET FOR DIAGNOSIS:

Number: {state['ticket_number']}
Title: {state['title']}
Description: {state['description']}
Category: {category}
Priority: {triage.get('priority', state['priority'])}
SLA: {sla_status}
Triage hypothesis: {triage.get('root_cause_hypothesis', 'N/A')}
Affected components: {triage.get('affected_components', [])}

KNOWLEDGE BASE RESULTS:
{kb_context}

WEB SEARCH RESULTS:
{web_context}

Produce your diagnosis JSON now."""

    messages = [
        SystemMessage(content=DIAGNOSIS_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    response = await llm.ainvoke(messages)

    try:
        diagnosis_result = json.loads(response.content)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\{.*\}", response.content, re.DOTALL)
        diagnosis_result = json.loads(match.group()) if match else {
            "root_cause": "Diagnosis parsing failed",
            "confidence": 0.2,
            "evidence": [],
            "resolution_steps": [],
            "verification_steps": [],
            "prevention": "",
            "estimated_resolution_time": "Unknown",
            "escalation_needed": True,
            "escalation_reason": "Automatic diagnosis failed",
            "related_tickets": [],
            "references": [],
        }

    # Record what the diagnosis was based on from the actual search results,
    # not from the LLM's own "references" (which it can make up)
    if kb.error:
        kb_status = "error"
    elif kb.is_strong:
        kb_status = "match"
    else:
        kb_status = "weak" if kb.docs else "none"
    if web is None:
        web_status = "not_needed"
    elif not web.ok:
        web_status = "error"
    else:
        web_status = "results" if web.results else "none"

    kb_sources = kb.sources if kb_status == "match" else []
    diagnosis_result["knowledge_sources"] = kb_sources
    diagnosis_result["web_sources"] = web_sources
    diagnosis_result["search_summary"] = {
        "knowledge_base": kb_status,
        "knowledge_base_top_score": round(kb.top_score, 3) if kb.top_score is not None else None,
        "web": web_status,
        "web_error": web.error if web and not web.ok else None,
    }
    if kb_sources:
        diagnosis_result["answer_basis"] = "knowledge_base"
    elif web_sources:
        diagnosis_result["answer_basis"] = "web"
    else:
        diagnosis_result["answer_basis"] = "model_general_knowledge"

    # Build human-readable resolution notes
    steps = diagnosis_result.get("resolution_steps", [])
    resolution_text = f"Root Cause: {diagnosis_result.get('root_cause', '')}\n\n"
    if steps:
        resolution_text += "Resolution Steps:\n"
        for s in steps:
            command = str(s.get("command") or "").strip()
            has_cmd = command.lower() not in ("", "n/a", "na", "none", "-", "null")
            cmd = f"\n  Command: `{command}`" if has_cmd else ""
            resolution_text += f"{s.get('step', '-')}. {s.get('action', '')}{cmd}\n"
    resolution_text += "\n" + _sources_text(kb_sources, web_sources)

    escalation_needed = diagnosis_result.get("escalation_needed", False)

    return {
        **state,
        "diagnosis_result": diagnosis_result,
        "final_resolution": resolution_text,
        "next": "human_review" if escalation_needed else "knowledge_agent",
        "hitl_required": escalation_needed or state.get("hitl_required", False),
        "hitl_reason": diagnosis_result.get("escalation_reason", "") if escalation_needed else state.get("hitl_reason", ""),
        "iteration": state.get("iteration", 0) + 1,
        "messages": [
            HumanMessage(
                content=f"[Diagnosis Agent] Root cause: {diagnosis_result.get('root_cause', '')} "
                        f"(confidence: {diagnosis_result.get('confidence', 0):.0%})"
            )
        ],
    }
