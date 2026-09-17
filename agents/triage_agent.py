"""
NEXUS AI · agents/triage_agent.py
Triage Agent — first responder in the LangGraph pipeline.

Responsibilities:
  - Classify the ticket by category and validate user-submitted priority
  - Extract key entities (error codes, affected systems, urgency signals)
  - Decide whether HITL (human escalation) is needed
  - Route to the correct next agent(s)
  - Write structured triage_result back to TicketState
"""

import json
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import TicketState
from agents.tools.ticket_tools import check_sla_status
from rag.config import rag_settings


TRIAGE_SYSTEM_PROMPT = """You are the Triage Agent for NEXUS AI IT Service Management.

Your job is to analyse an incoming IT ticket and produce a structured JSON triage assessment.

Analyse the ticket and respond with ONLY valid JSON (no markdown, no explanation) in this exact schema:
{
  "category": "network|hardware|software|security|access|performance|database|other",
  "priority": "low|medium|high|critical",
  "priority_override": true/false,
  "priority_override_reason": "string or null",
  "root_cause_hypothesis": "brief hypothesis about the likely cause",
  "affected_components": ["list", "of", "affected", "systems"],
  "keywords": ["extracted", "technical", "keywords"],
  "urgency_signals": ["signals", "indicating", "urgency"],
  "hitl_required": true/false,
  "hitl_reason": "string or null",
  "next_agent": "diagnosis_agent|security_agent|knowledge_agent",
  "confidence": 0.0-1.0
}

Rules:
- Set priority_override=true if the ticket text signals higher urgency than the user selected
- Set hitl_required=true for: data loss risk, production outage > 1h, security breach, or confidence < 0.4
- next_agent should be 'security_agent' if security keywords are present, else 'diagnosis_agent'
"""


async def triage_agent(state: TicketState) -> TicketState:
    """
    LangGraph node: Triage Agent.
    Reads ticket from state, produces structured triage_result, routes to next agent.
    """
    llm = ChatOpenAI(
        model=rag_settings.llm_model,
        temperature=0.0,
        api_key=rag_settings.openai_api_key,
    )

    # Check SLA status as additional context
    sla_context = await check_sla_status.ainvoke({"ticket_id": state["ticket_id"]})

    user_prompt = f"""TICKET TO TRIAGE:

Ticket Number: {state['ticket_number']}
Title: {state['title']}
Description: {state['description']}
User-submitted Category: {state['category']}
User-submitted Priority: {state['priority']}
SLA Status: {sla_context}

Produce your triage assessment JSON now."""

    messages = [
        SystemMessage(content=TRIAGE_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    response = await llm.ainvoke(messages)

    # Parse structured output
    try:
        triage_result = json.loads(response.content)
    except json.JSONDecodeError:
        # Fallback: extract JSON from response if wrapped in text
        import re
        match = re.search(r"\{.*\}", response.content, re.DOTALL)
        if match:
            triage_result = json.loads(match.group())
        else:
            triage_result = {
                "category": state["category"],
                "priority": state["priority"],
                "priority_override": False,
                "root_cause_hypothesis": "Could not parse triage output",
                "affected_components": [],
                "keywords": [],
                "urgency_signals": [],
                "hitl_required": False,
                "hitl_reason": None,
                "next_agent": "diagnosis_agent",
                "confidence": 0.3,
            }

    # Determine next node
    next_node = triage_result.get("next_agent", "diagnosis_agent")
    hitl_required = triage_result.get("hitl_required", False)

    return {
        **state,
        "triage_result": triage_result,
        "next": "human_review" if hitl_required else next_node,
        "hitl_required": hitl_required,
        "hitl_reason": triage_result.get("hitl_reason", ""),
        "iteration": state.get("iteration", 0) + 1,
        "messages": [
            HumanMessage(
                content=f"[Triage Agent] Assessment complete. "
                        f"Priority: {triage_result.get('priority')} | "
                        f"Next: {next_node} | "
                        f"HITL: {hitl_required}"
            )
        ],
    }
