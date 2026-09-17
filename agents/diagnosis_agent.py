"""
NEXUS AI · agents/diagnosis_agent.py
Diagnosis Agent — RAG-powered root cause analysis.

Uses: rag_search, web_search, check_sla_status
Produces: structured diagnosis with step-by-step resolution plan
"""

import json
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import TicketState
from agents.tools.rag_tool import rag_search
from agents.tools.search_tools import web_search
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

Be specific. Include actual commands where relevant. Base your answer on the KB context provided."""


async def diagnosis_agent(state: TicketState) -> TicketState:
    """LangGraph node: Diagnosis Agent."""
    llm = ChatOpenAI(
        model=rag_settings.llm_model,
        temperature=0.0,
        api_key=rag_settings.openai_api_key,
    )

    triage = state.get("triage_result", {})
    category = triage.get("category", state["category"])
    keywords = triage.get("keywords", [])

    # ── Tool calls ────────────────────────────────────────────
    # 1. RAG search using triage keywords
    search_query = f"{state['title']} {' '.join(keywords[:3])} {category}"
    kb_context = await rag_search.ainvoke({
        "query": search_query,
        "category_filter": category,
        "top_k": 3,
    })

    # 2. Web search for additional context
    web_context = await web_search.ainvoke({
        "query": f"{state['title']} solution fix {category}",
        "max_results": 2,
    })

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

    # Build human-readable resolution notes
    steps = diagnosis_result.get("resolution_steps", [])
    resolution_text = f"Root Cause: {diagnosis_result.get('root_cause', '')}\n\n"
    if steps:
        resolution_text += "Resolution Steps:\n"
        for s in steps:
            cmd = f"\n  Command: `{s['command']}`" if s.get("command") else ""
            resolution_text += f"{s['step']}. {s['action']}{cmd}\n"

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
