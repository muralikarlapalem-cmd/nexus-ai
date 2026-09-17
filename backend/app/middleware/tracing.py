"""
NEXUS AI · app/middleware/tracing.py
Observability layer:
  - LangSmith callbacks for LLM call tracing (Phase 3 agents)
  - OpenTelemetry spans for FastAPI request tracing
  - Prometheus metrics for RAG latency, agent run count, eval scores
"""

import time
from contextlib import asynccontextmanager

from loguru import logger

from app.config import settings


# ── LangSmith ──────────────────────────────────────────────────
def get_langsmith_callbacks():
    """
    Return LangSmith tracing callbacks if configured.
    Pass these to any LangChain / LangGraph ainvoke() call.

    Usage:
        callbacks = get_langsmith_callbacks()
        await llm.ainvoke(messages, config={"callbacks": callbacks})
    """
    if not settings.langsmith_api_key or not settings.langchain_tracing_v2:
        return []
    try:
        from langsmith import Client
        from langchain_core.tracers import LangChainTracer
        tracer = LangChainTracer(
            project_name=settings.langsmith_project,
            client=Client(api_key=settings.langsmith_api_key),
        )
        return [tracer]
    except ImportError:
        logger.warning("langsmith not installed — tracing disabled")
        return []
    except Exception as e:
        logger.warning(f"LangSmith init failed: {e}")
        return []


# ── Prometheus metrics ─────────────────────────────────────────
try:
    from prometheus_client import Counter, Histogram, Gauge

    TICKET_CREATED    = Counter("nexus_tickets_created_total", "Total tickets created", ["priority", "category"])
    AGENT_RUN_COUNT   = Counter("nexus_agent_runs_total", "Total agent pipeline runs", ["status"])
    RAG_QUERY_LATENCY = Histogram("nexus_rag_query_seconds", "RAG query latency", buckets=[0.1, 0.5, 1, 2, 5, 10])
    EVAL_SCORE        = Gauge("nexus_eval_score", "Latest evaluation score", ["metric"])
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False
    logger.warning("prometheus_client not installed — metrics disabled")


@asynccontextmanager
async def track_rag_latency():
    """Context manager to measure and record RAG query duration."""
    start = time.perf_counter()
    try:
        yield
    finally:
        if _METRICS_AVAILABLE:
            duration = time.perf_counter() - start
            RAG_QUERY_LATENCY.observe(duration)


def record_ticket_created(priority: str, category: str):
    if _METRICS_AVAILABLE:
        TICKET_CREATED.labels(priority=priority, category=category).inc()


def record_agent_run(status: str = "success"):
    if _METRICS_AVAILABLE:
        AGENT_RUN_COUNT.labels(status=status).inc()


def record_eval_score(metric: str, score: float):
    if _METRICS_AVAILABLE:
        EVAL_SCORE.labels(metric=metric).set(score)
