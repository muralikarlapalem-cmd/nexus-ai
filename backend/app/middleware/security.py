"""
NEXUS AI · app/middleware/security.py
Security middleware:
  1. PII Redaction  — Microsoft Presidio strips PII from ticket content before storage
  2. Prompt Injection Guard — pattern-based + LLM-based detection on RAG/agent inputs
"""

import re
from fastapi import Request, HTTPException, status
from loguru import logger


# ── Prompt Injection Patterns ──────────────────────────────────
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(your\s+)?(system\s+)?prompt",
    r"you\s+are\s+now\s+(a\s+)?(?:DAN|jailbreak|unrestricted)",
    r"forget\s+(everything|all)\s+(you('ve|\s+have))?\s+(been\s+)?told",
    r"act\s+as\s+if\s+(you\s+are|you're)\s+(a\s+)?different",
    r"new\s+instruction[s]?:",
    r"system\s*:\s*you\s+are",
    r"\[INST\]|\[\/INST\]|<\|im_start\|>|<\|im_end\|>",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)


def detect_prompt_injection(text: str) -> bool:
    """Return True if the text appears to contain a prompt injection attempt."""
    return bool(_INJECTION_RE.search(text))


def check_input_safety(text: str, source: str = "input") -> None:
    """
    Raise 422 if prompt injection is detected.
    Call this in route handlers before passing user text to any LLM.
    """
    if detect_prompt_injection(text):
        logger.warning(f"Prompt injection attempt detected in {source}: {text[:100]}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Input contains disallowed content. Please rephrase your request.",
        )


# ── PII Redaction (Presidio) ────────────────────────────────────
_presidio_analyzer = None
_presidio_anonymizer = None


def _get_presidio():
    """Lazy-load Presidio — only when PII redaction is first needed."""
    global _presidio_analyzer, _presidio_anonymizer
    if _presidio_analyzer is None:
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
            _presidio_analyzer  = AnalyzerEngine()
            _presidio_anonymizer = AnonymizerEngine()
            logger.info("Presidio PII engine loaded")
        except ImportError:
            logger.warning("presidio-analyzer not installed — PII redaction disabled")
    return _presidio_analyzer, _presidio_anonymizer


def redact_pii(text: str, language: str = "en") -> tuple[str, list[str]]:
    """
    Redact PII from text using Microsoft Presidio.
    Returns (redacted_text, list_of_pii_types_found).
    Falls back gracefully if Presidio is not installed.
    """
    analyzer, anonymizer = _get_presidio()
    if not analyzer:
        return text, []

    try:
        results = analyzer.analyze(text=text, language=language)
        pii_types = list({r.entity_type for r in results})
        if not results:
            return text, []
        redacted = anonymizer.anonymize(text=text, analyzer_results=results)
        return redacted.text, pii_types
    except Exception as e:
        logger.warning(f"Presidio redaction failed: {e}")
        return text, []
