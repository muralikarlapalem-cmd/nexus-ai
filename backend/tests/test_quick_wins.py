"""
NEXUS AI · tests/test_quick_wins.py
Roadmap quick wins 1-4: PII redaction, Prometheus metrics, LangSmith run IDs,
OpenTelemetry tracing.
"""

import os
import subprocess
import sys
import textwrap
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.core.security import create_access_token, hash_password
from app.database import Base, get_async_db
from app.main import app
from app.middleware import security, tracing
from app.models.audit_log import AuditLog
from app.models.ticket import Ticket
from app.models.user import User, UserRole

TEST_DB_URL = "postgresql+asyncpg://nexus:nexus_secret@localhost:15432/nexusai_test"
test_engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    app.dependency_overrides[get_async_db] = override_get_db
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def agent_token(setup_db):
    async with TestSessionLocal() as db:
        user = User(email="qw@nexus.ai", username="qw_agent", full_name="QW Agent",
                    role=UserRole.AGENT, hashed_password=hash_password("testpass123"))
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return create_access_token(subject=str(user.id), extra_claims={"role": "agent", "email": user.email})


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ─────────────────────────────────────────────────────────────
#  1. PII redaction
# ─────────────────────────────────────────────────────────────
def test_personal_data_is_redacted():
    text = ("Contact John Smith at john.smith@acme.com or +91 98450 12345. "
            "PAN ABCDE1234F, card 4111 1111 1111 1111.")
    redacted, found = security.redact_pii(text)
    for secret in ("John Smith", "john.smith@acme.com", "98450 12345", "ABCDE1234F", "4111 1111 1111 1111"):
        assert secret not in redacted
    assert {"PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IN_PAN", "CREDIT_CARD"} <= set(found)


def test_technical_details_are_kept():
    """IPs, host names, error codes and times are what the agents diagnose with."""
    text = ("ORA-00060 deadlock detected on prod-db-01 (10.20.30.40) since 10:15. "
            "Pod api-7f9c restarted; see https://grafana.internal/d/abc. Disk /u01 at 98%.")
    redacted, found = security.redact_pii(text)
    assert redacted == text
    assert found == []


def test_redaction_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "pii_redaction_enabled", False)
    assert security.redact_pii("mail jane@acme.com") == ("mail jane@acme.com", [])


@pytest.mark.asyncio
async def test_new_ticket_is_stored_redacted_and_audited(agent_token):
    async with client() as c:
        resp = await c.post("/api/v1/tickets/", headers={"Authorization": f"Bearer {agent_token}"}, json={
            "title": "VPN fails for priya.sharma@acme.com",
            "description": "Priya Sharma (+91 98450 12345) cannot connect to vpn-gw-02 at 10.1.2.3 since 09:30.",
            "category": "network", "priority": "high",
        })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "priya.sharma@acme.com" not in body["title"] and "<EMAIL_ADDRESS>" in body["title"]
    assert "98450" not in body["description"]
    assert "vpn-gw-02" in body["description"] and "10.1.2.3" in body["description"]

    async with TestSessionLocal() as db:
        log = (await db.execute(select(AuditLog).where(AuditLog.action == "ticket.create"))).scalar_one()
    assert {"EMAIL_ADDRESS", "PHONE_NUMBER"} <= set(log.details["pii_redacted"])


def test_imported_tickets_are_redacted():
    from app.integrations.schemas import ExternalTicket
    from app.integrations.sync import _redacted_text

    ext = ExternalTicket(external_id="1", title="Outlook crash for rahul@acme.com",
                         description="Call 4111 1111 1111 1111? no: call +1 415 555 0100")
    title, description, found = _redacted_text(ext)
    assert "rahul@acme.com" not in title
    assert "415 555 0100" not in description and "4111" not in description
    assert "EMAIL_ADDRESS" in found


@pytest.mark.asyncio
async def test_ai_query_is_redacted_before_the_llm(agent_token, monkeypatch):
    from app.api.v1 import rag as rag_api

    seen = {}

    async def fake_rag_query(query, **_):
        seen["query"] = query
        return {"answer": "ok", "sources": [], "basis": "kb", "query": query, "docs_retrieved": 0}

    monkeypatch.setattr(rag_api, "RAG_AVAILABLE", True)
    monkeypatch.setattr(rag_api, "rag_query", fake_rag_query, raising=False)
    async with client() as c:
        resp = await c.post("/api/v1/rag/query", headers={"Authorization": f"Bearer {agent_token}"},
                            json={"query": "Why can't anil.k@acme.com reach 10.0.0.5?"})
    assert resp.status_code == 200, resp.text
    assert "anil.k@acme.com" not in seen["query"] and "10.0.0.5" in seen["query"]


# ─────────────────────────────────────────────────────────────
#  2. Prometheus metrics
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_metrics_endpoint(agent_token):
    async with client() as c:
        await c.post("/api/v1/tickets/", headers={"Authorization": f"Bearer {agent_token}"}, json={
            "title": "Printer offline on floor 3", "description": "Printer PRN-3F is offline since morning.",
            "category": "hardware", "priority": "low",
        })
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert 'nexus_tickets_created_total{category="hardware",priority="low",source="nexus"}' in text
    assert "nexus_http_requests_total" in text
    assert 'path="/api/v1/tickets/"' in text
    assert "/metrics" not in [line.split('path="')[1].split('"')[0]
                              for line in text.splitlines() if line.startswith("nexus_http_requests_total{")]


@pytest.mark.asyncio
async def test_known_series_start_at_zero():
    """A counter that first appears at 1 is invisible to increase(); so known series start at 0."""
    async with client() as c:
        text = (await c.get("/metrics")).text
    # Present before anything of that kind happened (other tests may have counted some already)
    assert 'nexus_pii_redactions_total{entity="IBAN_CODE"}' in text
    assert 'nexus_agent_runs_total{outcome="needs_human"}' in text
    assert 'nexus_tickets_created_total{category="database",priority="critical",source="servicenow"}' in text


@pytest.mark.parametrize("state, outcome", [
    ({"final_resolution": "fixed"}, "resolved"),
    ({"final_resolution": "", "hitl_required": True}, "needs_human"),
    ({"__interrupt__": [1], "final_resolution": "x"}, "needs_human"),
    ({"error": "boom"}, "error"),
    (None, "error"),
])
def test_agent_run_outcome(state, outcome):
    assert tracing.agent_run_outcome(state) == outcome


def test_worker_metrics_are_combined_across_processes(tmp_path):
    """The Celery worker's child processes write to PROMETHEUS_MULTIPROC_DIR; one endpoint sums them."""
    script = textwrap.dedent("""
        import multiprocessing as mp
        from app.middleware.worker_metrics import start_worker_metrics_server

        def child():
            from app.middleware import tracing
            tracing.record_agent_run("resolved", 12.0)

        if __name__ == "__main__":
            assert start_worker_metrics_server(0)   # clears the folder; port 0 = any free port
            ps = [mp.get_context("fork").Process(target=child) for _ in range(3)]
            [p.start() for p in ps]; [p.join() for p in ps]
            assert all(p.exitcode == 0 for p in ps)
            from prometheus_client import CollectorRegistry, generate_latest, multiprocess
            reg = CollectorRegistry(); multiprocess.MultiProcessCollector(reg)
            print(generate_latest(reg).decode())
    """)
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path / "prom")}
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(__file__)), timeout=120)
    assert out.returncode == 0, out.stderr
    assert 'nexus_agent_runs_total{outcome="resolved"} 3.0' in out.stdout
    assert "nexus_agent_run_seconds_count 3.0" in out.stdout
    # Created at 0 by the main process before any run, so Prometheus sees the first one
    assert 'nexus_agent_runs_total{outcome="error"} 0.0' in out.stdout


# ─────────────────────────────────────────────────────────────
#  3. LangSmith run IDs
# ─────────────────────────────────────────────────────────────
def test_run_config_sets_a_known_run_id(monkeypatch):
    from agents import supervisor

    monkeypatch.setattr(settings, "langsmith_api_key", "ls-key")
    monkeypatch.setattr(settings, "langchain_tracing_v2", True)
    config, run_id = supervisor._run_config("t-1", "NX-2026-00001", "run")
    assert str(config["run_id"]) == run_id
    assert config["run_name"] == "NEXUS NX-2026-00001 (run)"
    assert config["metadata"]["ticket_number"] == "NX-2026-00001"
    assert config["configurable"]["thread_id"] == "t-1"

    monkeypatch.setattr(settings, "langchain_tracing_v2", False)
    config, run_id = supervisor._run_config("t-1", "NX-2026-00001", "run")
    assert run_id is None and "run_id" not in config


@pytest.mark.asyncio
async def test_langsmith_link_endpoint(agent_token, monkeypatch):
    from app.api.v1 import agents as agents_api

    run_id = str(uuid.uuid4())
    async with TestSessionLocal() as db:
        creator = (await db.execute(select(User))).scalar_one()
        t = Ticket(ticket_number="NX-2026-09999", title="Traced ticket", description="d" * 20,
                   created_by_id=creator.id, langsmith_run_id=run_id)
        db.add(t)
        await db.commit()
        ticket_id = t.id

    monkeypatch.setattr(settings, "langsmith_api_key", "ls-key")
    monkeypatch.setattr(agents_api, "_langsmith_base",
                        lambda project: "https://smith.langchain.com/o/ORG/projects/p/PROJ")
    async with client() as c:
        resp = await c.get(f"/api/v1/agents/langsmith/{ticket_id}", headers={"Authorization": f"Bearer {agent_token}"})
    assert resp.status_code == 200
    assert resp.json() == {"run_id": run_id,
                           "url": f"https://smith.langchain.com/o/ORG/projects/p/PROJ/r/{run_id}?poll=true"}


def test_langsmith_base_url_is_built_from_project(monkeypatch):
    """_langsmith_base asks LangSmith for the project once, then builds run URLs itself."""
    import langsmith
    from app.api.v1 import agents as agents_api

    calls = []

    class FakeClient:
        def __init__(self, **_): pass

        def read_project(self, project_name):
            calls.append(project_name)
            return SimpleNamespace(id="PROJ-ID")

        def get_run_url(self, run, project_id):
            return f"https://smith.langchain.com/o/ORG/projects/p/{project_id}/r/{run.id}?poll=true"

    monkeypatch.setattr(langsmith, "Client", FakeClient)
    agents_api._langsmith_project_cache.clear()
    assert agents_api._langsmith_base("nexus-ai") == "https://smith.langchain.com/o/ORG/projects/p/PROJ-ID"
    agents_api._langsmith_base("nexus-ai")
    assert calls == ["nexus-ai"]
    agents_api._langsmith_project_cache.clear()


# ─────────────────────────────────────────────────────────────
#  4. OpenTelemetry
# ─────────────────────────────────────────────────────────────
def test_opentelemetry_traces_requests_and_audit_logs(monkeypatch):
    """With an endpoint set, requests become spans and audit rows get the trace ID."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.exporter.zipkin import json as zipkin_json
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(zipkin_json, "ZipkinExporter", lambda endpoint: exporter)
    monkeypatch.setattr(settings, "otel_zipkin_endpoint", "http://jaeger:9411/api/v2/spans")

    demo = FastAPI()
    seen = {}

    @demo.get("/ping")
    def ping():
        seen["ids"] = tracing.current_trace_ids()
        row = AuditLog(action="demo", resource_type="demo")
        tracing.fill_audit_trace_ids(None, None, row)   # the before_insert hook
        seen["row"] = (row.trace_id, row.span_id)
        return {"ok": True}

    from app.database import engine
    tracing.setup_opentelemetry(demo, engine)
    assert TestClient(demo).get("/ping").status_code == 200

    from sqlalchemy import event
    assert event.contains(AuditLog, "before_insert", tracing.fill_audit_trace_ids)
    trace_id, span_id = seen["ids"]
    assert trace_id and len(trace_id) == 32
    assert seen["row"] == (trace_id, span_id)
    from opentelemetry import trace as ot_trace
    ot_trace.get_tracer_provider().force_flush()
    names = [s.name for s in exporter.get_finished_spans()]
    assert any("/ping" in n for n in names), names


def test_opentelemetry_off_without_endpoint(monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setattr(settings, "otel_zipkin_endpoint", "")
    demo = FastAPI()
    from app.database import engine
    tracing.setup_opentelemetry(demo, engine)
    assert not getattr(demo, "_is_instrumented_by_opentelemetry", False)
