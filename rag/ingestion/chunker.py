"""
NEXUS AI · rag/ingestion/chunker.py
Smart document chunking.

Strategy:
  - RecursiveCharacterTextSplitter for general documents
  - Heading-aware splitting for Markdown runbooks
  - Ticket documents are NOT re-chunked (already compact)
  - All chunks inherit + extend parent metadata
"""

import hashlib
import uuid
from typing import Sequence

from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from loguru import logger

from rag.config import rag_settings
from rag.ingestion.loaders import SOURCE_TICKET


# ─────────────────────────────────────────────────────────────
#  Splitters (instantiated once at module level)
# ─────────────────────────────────────────────────────────────

_recursive_splitter = RecursiveCharacterTextSplitter(
    chunk_size=rag_settings.chunk_size,
    chunk_overlap=rag_settings.chunk_overlap,
    length_function=len,
    separators=["\n\n", "\n", ". ", "! ", "? ", ", ", " ", ""],
    add_start_index=True,     # metadata: start_index in original text
)

_markdown_header_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[
        ("#", "h1"),
        ("##", "h2"),
        ("###", "h3"),
    ],
    strip_headers=False,       # keep headers in chunk content for context
)


def _make_chunk_id(content: str, metadata: dict) -> str:
    """Deterministic ID based on content + source — enables idempotent upserts."""
    raw = f"{metadata.get('file_path', '')}{metadata.get('start_index', 0)}{content[:100]}"
    return str(uuid.UUID(hashlib.md5(raw.encode()).hexdigest()))


def chunk_documents(
    documents: list[Document],
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[Document]:
    """
    Split a list of Documents into chunks.
    Tickets bypass chunking. Markdown gets heading-aware splits first.

    Returns a flat list of chunk Documents, each with:
      - chunk_id       (deterministic UUID for idempotent upserts)
      - chunk_index    (position within the parent document)
      - parent_source  (original file_path or ticket_id)
    """
    splitter = _recursive_splitter
    if chunk_size or chunk_overlap:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size or rag_settings.chunk_size,
            chunk_overlap=chunk_overlap or rag_settings.chunk_overlap,
            length_function=len,
            add_start_index=True,
        )

    all_chunks: list[Document] = []

    for doc in documents:
        source_type = doc.metadata.get("source_type", "")

        # ── Tickets: keep as-is (no re-chunk) ────────────────
        if source_type == SOURCE_TICKET:
            doc.metadata["chunk_id"] = _make_chunk_id(doc.page_content, doc.metadata)
            doc.metadata["chunk_index"] = 0
            doc.metadata["parent_source"] = doc.metadata.get("ticket_id", "")
            all_chunks.append(doc)
            continue

        # ── Markdown: heading-aware split first ───────────────
        if doc.metadata.get("file_name", "").endswith((".md", ".mdx")):
            try:
                md_chunks = _markdown_header_splitter.split_text(doc.page_content)
                # Then recursively split large sections
                sub_chunks = splitter.split_documents(md_chunks)
                source_chunks = sub_chunks
            except Exception:
                source_chunks = splitter.split_documents([doc])
        else:
            source_chunks = splitter.split_documents([doc])

        # ── Filter too-small chunks ───────────────────────────
        source_chunks = [
            c for c in source_chunks
            if len(c.page_content.strip()) >= rag_settings.min_chunk_size
        ]

        # ── Annotate with chunk metadata ──────────────────────
        parent_source = doc.metadata.get("file_path") or doc.metadata.get("ticket_id", "")
        for idx, chunk in enumerate(source_chunks):
            chunk.metadata.update({
                **doc.metadata,           # inherit all parent metadata
                **chunk.metadata,         # keep splitter-added metadata (headings etc.)
                "chunk_index": idx,
                "chunk_total": len(source_chunks),
                "parent_source": parent_source,
                "chunk_id": _make_chunk_id(chunk.page_content, {
                    "file_path": parent_source,
                    "start_index": idx,
            }),
            })

        all_chunks.extend(source_chunks)

    logger.info(
        f"Chunked {len(documents)} documents → {len(all_chunks)} chunks "
        f"(avg {len(all_chunks)//max(len(documents),1)} per doc)"
    )
    return all_chunks
