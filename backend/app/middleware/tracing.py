"""
NEXUS AI · app/middleware/tracing.py
Observability layer:
  - LangSmith callbacks for LLM call tracing (Phase 3 agents)
  - Prometheus metrics: API requests, tickets, agent runs, RAG latency,
    integration syncs, PII redactions (API at /metrics, worker on :9101)
  - OpenTelemetry spans for API requests and database queries (to Jaeger)
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
# The API serves its metrics at /metrics. The Celery worker runs agents in
# several processes, so it uses prometheus_client's multiprocess mode
# (PROMETHEUS_MULTIPROC_DIR, set in docker-compose) and serves the combined
# numbers on port 9101 (see app/middleware/worker_metrics.py).
try:
    from prometheus_client import Counter, Gauge, Histogram

    TICKET_CREATED = Counter(
        "nexus_tickets_created_total", "Tickets created", ["priority", "category", "source"],
    )
    AGENT_RUN_COUNT = Counter(
        "nexus_agent_runs_total", "Agent pipeline runs by outcome", ["outcome"],
    )
    AGENT_RUN_SECONDS = Histogram(
        "nexus_agent_run_seconds", "Agent pipeline run time",
        buckets=[5, 10, 20, 30, 45, 60, 90, 120, 180, 300],
    )
    RAG_QUERY_LATENCY = Histogram(
        "nexus_rag_query_seconds", "AI knowledge query time", buckets=[0.5, 1, 2, 3, 5, 8, 13, 20, 30],
    )
    INTEGRATION_SYNCS = Counter(
        "nexus_integration_syncs_total", "Integration syncs by connection and result", ["connection", "result"],
    )
    INTEGRATION_TICKETS = Counter(
        "nexus_integration_tickets_total", "Tickets read during integration syncs", ["connection", "outcome"],
    )
    PII_REDACTIONS = Counter(
        "nexus_pii_redactions_total", "Personal-data items redacted, by type", ["entity"],
    )
    EVAL_SCORE = Gauge("nexus_eval_score", "Latest evaluation score", ["metric"], multiprocess_mode="max")
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
            RAG_QUERY_LATENCY.observe(time.perf_counter() - start)


def record_ticket_created(priority: str, category: str, source: str = "nexus"):
    if _METRICS_AVAILABLE:
        TICKET_CREATED.labels(priority=priority, category=category, source=source).inc()


def record_agent_run(outcome: str = "resolved", seconds: float | None = None):
    """outcome: resolved | needs_human | error"""
    if _METRICS_AVAILABLE:
        AGENT_RUN_COUNT.labels(outcome=outcome).inc()
        if seconds is not None:
            AGENT_RUN_SECONDS.observe(seconds)


def agent_run_outcome(final_state: dict | None) -> str:
    """Classify a finished supervisor run for the agent-run metric."""
    if not final_state or final_state.get("error"):
        return "error"
    if final_state.get("__interrupt__") or final_state.get("hitl_required") or not final_state.get("final_resolution"):
        return "needs_human"
    return "resolved"


def record_integration_sync(connection: str, result: str, counts: dict | None = None):
    """result: success | partial | error. counts: {"created": n, "updated": n, ...}"""
    if not _METRICS_AVAILABLE:
        return
    INTEGRATION_SYNCS.labels(connection=connection, result=result).inc()
    for outcome, n in (counts or {}).items():
        if n:
            INTEGRATION_TICKETS.labels(connection=connection, outcome=outcome).inc(n)


def record_pii_redactions(entity_types: list[str]):
    if _METRICS_AVAILABLE:
        for entity in entity_types:
            PII_REDACTIONS.labels(entity=entity).inc()


def record_eval_score(metric: str, score: float):
    if _METRICS_AVAILABLE:
        EVAL_SCORE.labels(metric=metric).set(score)


def setup_metrics(app) -> None:
    """Count every API request and serve all NEXUS metrics at /metrics."""
    try:
        from starlette_exporter import PrometheusMiddleware, handle_metrics
    except ImportError:
        logger.warning("starlette-exporter not installed — /metrics disabled")
        return
    app.add_middleware(
        PrometheusMiddleware,
        app_name="nexus-api",
        prefix="nexus_http",
        group_paths=True,                 # /tickets/{ticket_id}, not one series per ticket
        filter_unhandled_paths=True,      # ignore 404 scans
        skip_paths=["/metrics", "/api/v1/health", "/api/v1/health/ready"],
    )
    app.add_route("/metrics", handle_metrics, include_in_schema=False)


# ── OpenTelemetry ──────────────────────────────────────────────
def setup_opentelemetry(app, engine) -> None:
    """
    Trace every API request and database query, and send the traces to
    Jaeger (Zipkin JSON over HTTP). Off unless OTEL_ZIPKIN_ENDPOINT is set.
    The current trace and span IDs are also written onto audit-log rows.
    """
    if not settings.otel_zipkin_endpoint:
        logger.info("OpenTelemetry off (OTEL_ZIPKIN_ENDPOINT not set)")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.zipkin.json import ZipkinExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logger.warning(f"OpenTelemetry packages missing — tracing off ({e})")
        return

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: settings.otel_service_name}))
    provider.add_span_processor(BatchSpanProcessor(ZipkinExporter(endpoint=settings.otel_zipkin_endpoint)))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="metrics,api/v1/health",
    )
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)
    _link_audit_logs_to_traces()
    logger.info(f"OpenTelemetry tracing on → {settings.otel_zipkin_endpoint}")


def current_trace_ids() -> tuple[str | None, str | None]:
    """(trace_id, span_id) of the active OpenTelemetry span, as hex; (None, None) if none."""
    try:
        from opentelemetry import trace
        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except ImportError:
        pass
    return None, None


def fill_audit_trace_ids(_mapper, _connection, target) -> None:
    """SQLAlchemy before_insert hook: stamp an audit row with the current trace."""
    if not target.trace_id:
        target.trace_id, target.span_id = current_trace_ids()


def _link_audit_logs_to_traces() -> None:
    from sqlalchemy import event

    from app.models.audit_log import AuditLog

    if not event.contains(AuditLog, "before_insert", fill_audit_trace_ids):
        event.listen(AuditLog, "before_insert", fill_audit_trace_ids)
