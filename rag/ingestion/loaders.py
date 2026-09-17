"""
NEXUS AI · rag/ingestion/loaders.py
Document loaders — converts raw files into LangChain Documents with rich metadata.

Supported sources:
  - PDF        (runbooks, SLA policies, compliance docs)
  - Markdown   (runbooks stored in Git)
  - Plain text (exported logs, legacy docs)
  - IT Ticket  (resolved tickets from Postgres → training corpus for RAG)
  - Directory  (batch-load an entire folder)
"""

import re
from pathlib import Path
from typing import Any

from langchain_community.document_loaders import (
    DirectoryLoader,
    PyPDFLoader,
    TextLoader,
    UnstructuredMarkdownLoader,
)
from langchain_core.documents import Document
from loguru import logger


# ─────────────────────────────────────────────────────────────
#  Source-type constants — stored in doc.metadata["source_type"]
# ─────────────────────────────────────────────────────────────
SOURCE_RUNBOOK = "runbook"
SOURCE_POLICY = "policy"
SOURCE_TICKET = "resolved_ticket"
SOURCE_CVE = "cve"
SOURCE_GENERIC = "document"


def load_pdf(path: str | Path, metadata: dict | None = None) -> list[Document]:
    """
    Load a PDF file page-by-page using PyPDF.
    Each page becomes one Document with page_number metadata.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    loader = PyPDFLoader(str(path))
    docs = loader.load()

    base_meta = {
        "source_type": SOURCE_RUNBOOK,
        "file_name": path.name,
        "file_path": str(path),
        **(metadata or {}),
    }
    for doc in docs:
        doc.metadata.update(base_meta)

    logger.info(f"Loaded PDF: {path.name} → {len(docs)} pages")
    return docs


def load_markdown(path: str | Path, metadata: dict | None = None) -> list[Document]:
    """
    Load a Markdown file using Unstructured.
    Sections are preserved as separate documents where possible.
    """
    path = Path(path)
    loader = UnstructuredMarkdownLoader(str(path), mode="elements")
    docs = loader.load()

    base_meta = {
        "source_type": SOURCE_RUNBOOK,
        "file_name": path.name,
        "file_path": str(path),
        **(metadata or {}),
    }
    for doc in docs:
        doc.metadata.update(base_meta)

    logger.info(f"Loaded Markdown: {path.name} → {len(docs)} elements")
    return docs


def load_text(path: str | Path, metadata: dict | None = None) -> list[Document]:
    """Load a plain text file as a single Document."""
    path = Path(path)
    loader = TextLoader(str(path), encoding="utf-8")
    docs = loader.load()

    base_meta = {
        "source_type": SOURCE_GENERIC,
        "file_name": path.name,
        "file_path": str(path),
        **(metadata or {}),
    }
    for doc in docs:
        doc.metadata.update(base_meta)

    logger.info(f"Loaded text: {path.name}")
    return docs


def load_directory(
    directory: str | Path,
    glob: str = "**/*",
    metadata: dict | None = None,
) -> list[Document]:
    """
    Recursively load all supported files in a directory.
    Detects file type by extension and routes to the right loader.
    """
    directory = Path(directory)
    all_docs: list[Document] = []

    for path in directory.rglob("*"):
        if path.is_file():
            try:
                ext = path.suffix.lower()
                if ext == ".pdf":
                    all_docs.extend(load_pdf(path, metadata))
                elif ext in (".md", ".mdx"):
                    all_docs.extend(load_markdown(path, metadata))
                elif ext in (".txt", ".log"):
                    all_docs.extend(load_text(path, metadata))
                # Skip unsupported extensions silently
            except Exception as e:
                logger.warning(f"Could not load {path}: {e}")

    logger.info(f"Directory load complete: {directory} → {len(all_docs)} documents")
    return all_docs


def load_ticket_as_document(ticket: dict[str, Any]) -> Document:
    """
    Convert a resolved IT ticket (dict from Postgres) into a LangChain Document.
    This is the primary way the RAG corpus learns from historical resolutions.

    Expected ticket keys: id, ticket_number, title, description,
                          category, priority, resolution_notes,
                          triage_output, created_at, resolved_at
    """
    # Build a rich text representation of the ticket
    parts = [
        f"TICKET: {ticket.get('ticket_number', 'UNKNOWN')}",
        f"TITLE: {ticket.get('title', '')}",
        f"CATEGORY: {ticket.get('category', '')}",
        f"PRIORITY: {ticket.get('priority', '')}",
        "",
        "PROBLEM DESCRIPTION:",
        ticket.get("description", ""),
        "",
    ]

    triage = ticket.get("triage_output") or {}
    if triage:
        parts += [
            "TRIAGE ASSESSMENT:",
            f"Root cause hypothesis: {triage.get('root_cause', '')}",
            f"Affected components: {', '.join(triage.get('components', []))}",
            "",
        ]

    if ticket.get("resolution_notes"):
        parts += [
            "RESOLUTION:",
            ticket["resolution_notes"],
        ]

    content = "\n".join(parts)

    metadata = {
        "source_type": SOURCE_TICKET,
        "ticket_id": str(ticket.get("id", "")),
        "ticket_number": ticket.get("ticket_number", ""),
        "category": ticket.get("category", ""),
        "priority": ticket.get("priority", ""),
        "resolved_at": str(ticket.get("resolved_at", "")),
        "tenant_id": ticket.get("tenant_id", "default"),
    }

    return Document(page_content=content, metadata=metadata)


def load_tickets_batch(tickets: list[dict[str, Any]]) -> list[Document]:
    """Convert a list of resolved ticket dicts into Documents."""
    docs = [load_ticket_as_document(t) for t in tickets]
    logger.info(f"Loaded {len(docs)} resolved tickets as RAG documents")
    return docs
