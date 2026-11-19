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
# Only personal data is redacted. IP addresses, hostnames, URLs, dates and
# error codes are left alone: the agents need them to diagnose the ticket.
# The list and the spaCy model can be changed in .env (PII_ENTITIES, PII_SPACY_MODEL).
_presidio_analyzer = None
_presidio_anonymizer = None
_presidio_failed = False


def _get_presidio():
    """Lazy-load Presidio the first time PII redaction is needed (about 1 s)."""
    global _presidio_analyzer, _presidio_anonymizer, _presidio_failed
    if _presidio_analyzer is None and not _presidio_failed:
        try:
            from app.config import settings
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            from presidio_anonymizer import AnonymizerEngine

            nlp_engine = NlpEngineProvider(nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": settings.pii_spacy_model}],
            }).create_engine()
            _presidio_analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
            _presidio_anonymizer = AnonymizerEngine()
            logger.info(f"Presidio PII engine loaded ({settings.pii_spacy_model})")
        except Exception as e:  # missing package or spaCy model
            _presidio_failed = True
            logger.warning(f"PII redaction disabled: could not load Presidio ({e})")
    return _presidio_analyzer, _presidio_anonymizer


def warm_up_pii() -> None:
    """Load Presidio ahead of the first request (called from the startup warm-up)."""
    from app.config import settings
    if settings.pii_redaction_enabled:
        _get_presidio()


def _looks_like_identifier(text: str) -> bool:
    """spaCy sometimes tags error codes and host names as people (ORA-00060, api-7f9c)."""
    return any(ch.isdigit() or ch in "-_/.:@\\" for ch in text)


def redact_pii(text: str, language: str = "en") -> tuple[str, list[str]]:
    """
    Redact personal data from text using Microsoft Presidio.
    Returns (redacted_text, sorted list of PII types found), e.g.
    ("Call <PERSON> on <PHONE_NUMBER>", ["PERSON", "PHONE_NUMBER"]).
    Returns the text unchanged if redaction is off or Presidio isn't available.
    """
    from app.config import settings

    if not text or not settings.pii_redaction_enabled:
        return text, []
    analyzer, anonymizer = _get_presidio()
    if not analyzer:
        return text, []

    try:
        results = analyzer.analyze(
            text=text,
            language=language,
            entities=settings.pii_entities,
            score_threshold=settings.pii_score_threshold,
        )
        results = [
            r for r in results
            if not (r.entity_type == "PERSON" and _looks_like_identifier(text[r.start:r.end]))
        ]
        if not results:
            return text, []
        redacted = anonymizer.anonymize(text=text, analyzer_results=results)
        from app.middleware.tracing import record_pii_redactions
        record_pii_redactions([r.entity_type for r in results])
        return redacted.text, sorted({r.entity_type for r in results})
    except Exception as e:
        logger.warning(f"Presidio redaction failed: {e}")
        return text, []


def redact_fields(**fields: str | None) -> tuple[dict[str, str | None], list[str]]:
    """
    Redact several text fields at once.
    Returns ({field: redacted text}, sorted list of all PII types found).
    """
    found: set[str] = set()
    out: dict[str, str | None] = {}
    for name, value in fields.items():
        if value:
            value, types = redact_pii(value)
            found.update(types)
        out[name] = value
    return out, sorted(found)


def redact_documents(docs: list) -> list[str]:
    """
    Redact personal data in LangChain Documents in place (before they're indexed
    into the knowledge base). Returns the sorted list of PII types found.
    """
    found: set[str] = set()
    for doc in docs:
        doc.page_content, types = redact_pii(doc.page_content)
        found.update(types)
    if found:
        logger.info(f"PII redacted from {len(docs)} knowledge-base document(s): {sorted(found)}")
    return sorted(found)
