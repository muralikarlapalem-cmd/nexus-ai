"""
NEXUS AI · rag/seed.py
Seed script — loads sample runbooks into Qdrant + BM25 on first run.

Usage (from project root):
    cd backend
    python -m rag.seed

Run this once after Phase 2 setup to have searchable content immediately.
"""

import asyncio
from pathlib import Path

from loguru import logger


async def seed():
    from rag.ingestion.loaders import load_directory
    from rag.pipeline import ingest_documents

    sample_docs_path = Path(__file__).parent / "sample_docs"
    if not sample_docs_path.exists():
        logger.error(f"Sample docs directory not found: {sample_docs_path}")
        return

    logger.info(f"Seeding knowledge base from: {sample_docs_path}")
    docs = load_directory(
        sample_docs_path,
        metadata={"source_type": "runbook", "category": "operations", "tenant_id": "default"},
    )

    if not docs:
        logger.warning("No documents found in sample_docs/")
        return

    result = await ingest_documents(docs, tenant_id="default")
    logger.info(f"Seed complete: {result}")


if __name__ == "__main__":
    asyncio.run(seed())
