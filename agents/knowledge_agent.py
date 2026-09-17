"""
NEXUS AI · agents/knowledge_agent.py
Knowledge Agent — closes the learning loop.

After Diagnosis Agent produces a resolution:
  1. Generates a structured runbook from the resolution
  2. Upserts it into Qdrant (so future tickets find it)
  3. Updates the ticket in Postgres with resolution notes
  4. Identifies knowledge gaps (categories with no runbooks)
"""

from langchain_core.messages import HumanMessage

from agents.state import TicketState
from agents.tools.rag_tool import generate_runbook
from agents.tools.ticket_tools import update_ticket_field
from rag.config import rag_settings


async def knowledge_agent(state: TicketState) -> TicketState:
    """LangGraph node: Knowledge Agent."""

    diagnosis = state.get("diagnosis_result", {})
    resolution = state.get("final_resolution", "")

    knowledge_result: dict = {
        "runbook_generated": False,
        "runbook_content": "",
        "rag_upserted": False,
        "ticket_updated": False,
        "gaps_identified": [],
    }

    # ── 1. Generate runbook if diagnosis was confident ────────
    confidence = diagnosis.get("confidence", 0)
    if confidence >= 0.6 and diagnosis.get("resolution_steps"):
        steps_text = "\n".join(
            f"{s['step']}. {s['action']}" + (f"\n   `{s['command']}`" if s.get("command") else "")
            for s in diagnosis.get("resolution_steps", [])
        )

        runbook_content = await generate_runbook.ainvoke({
            "ticket_summary": f"{state['title']}: {diagnosis.get('root_cause', '')}",
            "resolution_steps": steps_text,
            "category": state.get("category", "other"),
        })

        knowledge_result["runbook_generated"] = True
        knowledge_result["runbook_content"] = runbook_content

        # ── 2. Upsert generated runbook into RAG ─────────────
        try:
            from langchain_core.documents import Document
            from rag.pipeline import ingest_documents

            runbook_doc = Document(
                page_content=runbook_content,
                metadata={
                    "source_type": "runbook",
                    "file_name": f"auto_{state['ticket_number']}.md",
                    "category": state.get("category", "other"),
                    "generated_from_ticket": state["ticket_number"],
                    "tenant_id": "default",
                },
            )
            await ingest_documents([runbook_doc], tenant_id="default")
            knowledge_result["rag_upserted"] = True
        except Exception as e:
            knowledge_result["rag_error"] = str(e)

    # ── 3. Write resolution back to ticket ────────────────────
    if resolution:
        update_result = await update_ticket_field.ainvoke({
            "ticket_id": state["ticket_id"],
            "field": "resolution_notes",
            "value": resolution,
        })
        knowledge_result["ticket_updated"] = "Successfully" in update_result

        # Also persist diagnosis output
        import json
        await update_ticket_field.ainvoke({
            "ticket_id": state["ticket_id"],
            "field": "diagnosis_output",
            "value": json.dumps(diagnosis),
        })

    return {
        **state,
        "knowledge_result": knowledge_result,
        "next": "END",
        "iteration": state.get("iteration", 0) + 1,
        "messages": [
            HumanMessage(
                content=f"[Knowledge Agent] Runbook generated: {knowledge_result['runbook_generated']} | "
                        f"RAG updated: {knowledge_result['rag_upserted']} | "
                        f"Ticket updated: {knowledge_result['ticket_updated']}"
            )
        ],
    }
