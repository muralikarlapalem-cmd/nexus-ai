"""
NEXUS AI · fine_tuning/generate_dataset.py
Export resolved tickets from Postgres into an Alpaca-format JSONL
dataset for QLoRA fine-tuning.

Alpaca format:
  {"instruction": "...", "input": "...", "output": "..."}

Run:
    cd backend
    python -m fine_tuning.generate_dataset --output fine_tuning/data/nexus_train.jsonl --min-tickets 50
"""

import asyncio
import json
import argparse
from pathlib import Path
from datetime import datetime

from loguru import logger


SYSTEM_INSTRUCTION = (
    "You are NEXUS AI, an expert IT Service Management assistant. "
    "Analyse the IT ticket below and provide: "
    "(1) root cause, (2) step-by-step resolution, (3) prevention advice."
)


def ticket_to_training_sample(ticket: dict) -> dict | None:
    """Convert a resolved ticket dict to an Alpaca training sample."""
    # Require a meaningful resolution
    resolution = ticket.get("resolution_notes", "")
    if not resolution or len(resolution) < 50:
        return None

    instruction = SYSTEM_INSTRUCTION
    input_text = (
        f"Category: {ticket.get('category', 'other')}\n"
        f"Priority: {ticket.get('priority', 'medium')}\n"
        f"Title: {ticket.get('title', '')}\n"
        f"Description: {ticket.get('description', '')}"
    )

    # Enrich output with triage diagnosis if available
    triage = ticket.get("triage_output") or {}
    diagnosis = ticket.get("diagnosis_output") or {}

    output_parts = []
    if diagnosis.get("root_cause"):
        output_parts.append(f"Root Cause: {diagnosis['root_cause']}")
    output_parts.append(f"Resolution:\n{resolution}")
    if diagnosis.get("prevention"):
        output_parts.append(f"Prevention: {diagnosis['prevention']}")

    return {
        "instruction": instruction,
        "input": input_text,
        "output": "\n\n".join(output_parts),
    }


async def export_training_data(output_path: str, min_tickets: int = 50) -> dict:
    """Pull resolved tickets and write to JSONL."""
    from app.database import get_db_context
    from app.models.ticket import Ticket, TicketStatus
    from sqlalchemy import select

    async with get_db_context() as db:
        result = await db.execute(
            select(Ticket)
            .where(Ticket.status.in_([TicketStatus.RESOLVED, TicketStatus.CLOSED]))
            .where(Ticket.resolution_notes.isnot(None))
            .order_by(Ticket.resolved_at.desc())
        )
        tickets = result.scalars().all()

    logger.info(f"Found {len(tickets)} resolved tickets")

    samples = []
    for t in tickets:
        sample = ticket_to_training_sample({
            "id": str(t.id),
            "ticket_number": t.ticket_number,
            "title": t.title,
            "description": t.description,
            "category": t.category.value if t.category else "other",
            "priority": t.priority.value if t.priority else "medium",
            "resolution_notes": t.resolution_notes,
            "triage_output": t.triage_output,
            "diagnosis_output": t.diagnosis_output,
        })
        if sample:
            samples.append(sample)

    if len(samples) < min_tickets:
        logger.warning(
            f"Only {len(samples)} valid samples (min: {min_tickets}). "
            "Fine-tuning on < 50 samples risks overfitting. Consider synthetic augmentation."
        )

    # Write JSONL
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        for sample in samples:
            f.write(json.dumps(sample) + "\n")

    summary = {
        "total_tickets": len(tickets),
        "valid_samples": len(samples),
        "output_path": str(output),
        "timestamp": datetime.utcnow().isoformat(),
    }
    logger.info(f"Dataset exported: {summary}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="fine_tuning/data/nexus_train.jsonl")
    parser.add_argument("--min-tickets", type=int, default=50)
    args = parser.parse_args()
    result = asyncio.run(export_training_data(args.output, args.min_tickets))
    print(json.dumps(result, indent=2))
