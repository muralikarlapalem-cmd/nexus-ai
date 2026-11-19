"""
NEXUS AI · agents/security_agent.py
Security Agent — runs when triage detects security-related tickets.

Responsibilities:
  - CVE lookup for mentioned software/versions
  - Log scanning for anomaly patterns
  - PII detection in ticket content
  - Threat severity assessment
  - Automated remediation recommendations
  - Routes to diagnosis_agent after security check
"""

import json
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import TicketState
from agents.tools.search_tools import cve_lookup, scan_system_logs
from agents.tools.rag_tool import rag_search
from rag.config import rag_settings


SECURITY_SYSTEM_PROMPT = """You are the Security Agent for NEXUS AI.
You assess security-related IT tickets for threats, CVEs, and compliance issues.

Respond with ONLY valid JSON:
{
  "threat_level": "low|medium|high|critical",
  "threat_type": "vulnerability|breach|misconfiguration|access_violation|malware|other",
  "cves_found": ["CVE-XXXX-XXXXX list or empty"],
  "pii_detected": true/false,
  "pii_types": ["email|phone|ssn|credit_card etc or empty"],
  "immediate_actions": ["list of immediate actions to take"],
  "remediation_steps": ["ordered remediation steps"],
  "compliance_impact": ["GDPR|HIPAA|SOC2|PCI-DSS affected or empty"],
  "escalate_to_security_team": true/false,
  "escalation_reason": "string or null",
  "containment_required": true/false
}"""


# Simple PII pattern detection (Presidio integration in Phase 5)
PII_PATTERNS = [
    ("email", r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    ("phone", r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    ("ip_address", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ("credit_card", r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
]


def _detect_pii(text: str) -> tuple[bool, list[str]]:
    import re
    found_types = []
    for pii_type, pattern in PII_PATTERNS:
        if re.search(pattern, text):
            found_types.append(pii_type)
    return bool(found_types), found_types


async def security_agent(state: TicketState) -> TicketState:
    """LangGraph node: Security Agent."""
    llm = ChatOpenAI(
        model=rag_settings.llm_model,
        temperature=0.0,
        api_key=rag_settings.openai_api_key,
        timeout=rag_settings.llm_timeout_seconds,
        max_retries=rag_settings.llm_max_retries,
    )

    triage = state.get("triage_result", {})
    keywords = triage.get("keywords", [])
    full_text = f"{state['title']} {state['description']}"

    # ── Tool calls ────────────────────────────────────────────
    # 1. CVE lookup for mentioned technologies
    cve_context = ""
    security_keywords = [k for k in keywords if len(k) > 3][:2]
    if security_keywords:
        cve_context = await cve_lookup.ainvoke({"keyword": " ".join(security_keywords)})

    # 2. RAG search for related security incidents
    kb_context = await rag_search.ainvoke({
        "query": f"security vulnerability {state['title']}",
        "category_filter": "security",
        "top_k": 2,
    })

    # 3. PII detection
    pii_found, pii_types = _detect_pii(full_text)

    # ── LLM assessment ────────────────────────────────────────
    user_prompt = f"""SECURITY TICKET ASSESSMENT:

Title: {state['title']}
Description: {state['description']}
Keywords: {keywords}
PII Detected: {pii_found} (types: {pii_types})

CVE LOOKUP RESULTS:
{cve_context or 'No CVEs found'}

KNOWLEDGE BASE - RELATED SECURITY INCIDENTS:
{kb_context}

Produce your security assessment JSON now."""

    messages = [SystemMessage(content=SECURITY_SYSTEM_PROMPT), HumanMessage(content=user_prompt)]
    response = await llm.ainvoke(messages)

    try:
        security_result = json.loads(response.content)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\{.*\}", response.content, re.DOTALL)
        security_result = json.loads(match.group()) if match else {
            "threat_level": "medium",
            "threat_type": "other",
            "cves_found": [],
            "pii_detected": pii_found,
            "pii_types": pii_types,
            "immediate_actions": ["Escalate to security team"],
            "remediation_steps": [],
            "compliance_impact": [],
            "escalate_to_security_team": True,
            "escalation_reason": "Automated assessment failed",
            "containment_required": False,
        }

    # Enrich with our own PII detection
    security_result["pii_detected"] = security_result.get("pii_detected") or pii_found
    security_result["pii_types"] = list(set(security_result.get("pii_types", []) + pii_types))

    # Critical threats need HITL
    hitl_needed = (
        security_result.get("threat_level") == "critical"
        or security_result.get("escalate_to_security_team")
        or security_result.get("containment_required")
    )

    return {
        **state,
        "security_result": security_result,
        "next": "human_review" if hitl_needed else "diagnosis_agent",
        "hitl_required": hitl_needed,
        "hitl_reason": f"Security threat level: {security_result.get('threat_level')}",
        "iteration": state.get("iteration", 0) + 1,
        "messages": [
            HumanMessage(
                content=f"[Security Agent] Threat: {security_result.get('threat_level')} | "
                        f"CVEs: {security_result.get('cves_found', [])} | "
                        f"PII: {security_result.get('pii_detected')}"
            )
        ],
    }
