# Observability and privacy

Four things that make NEXUS AI's behaviour visible and keep personal data out of it:

| What | Where to see it | What it shows |
|---|---|---|
| **PII redaction** (Presidio) | Ticket text in NEXUS; audit log `pii_redacted` | Names, emails, phone, card, PAN, Aadhaar, SSN and IBAN numbers removed before storage or any LLM call |
| **Prometheus + Grafana** | http://localhost:3000 (dashboard "NEXUS AI") | Tickets, agent runs and outcomes, run times, AI query times, integration syncs, PII redactions, API traffic |
| **LangSmith trace links** | Ticket page → Agent Trace → **View in LangSmith** | The full LLM trace of that ticket's latest agent run |
| **OpenTelemetry + Jaeger** | http://localhost:16686 → service `nexus-api` | Every API request with its database queries and timings |
| **Loki + Alloy** (logs) | Grafana → NEXUS AI dashboard → Logs; or Explore → Loki | Every NEXUS container's log output, searchable; the Security agent reads it |

---

## 1. One-time setup on your machine

These changes add Python packages and three containers, so the images need one rebuild.

1. Add to `.env`:
   ```
   GRAFANA_ADMIN_PASSWORD=<any password you like>
   ```
   (`docker compose` refuses to start until this is set.)
2. Rebuild and start (Command Prompt, in the project folder). The first rebuild downloads
   PyTorch and all packages again, so allow 10–20 minutes (longer on a slow connection). Build `api`
   first: the other two use the same Dockerfile and then come straight from its cache instead of
   downloading everything in parallel. Later rebuilds reuse the PyTorch layer.
   ```
   docker compose build api
   docker compose build celery-worker celery-beat
   docker compose up -d
   ```
3. Check: `docker compose ps` also lists `nexus-prometheus`, `nexus-grafana` and `nexus-jaeger`.
4. Optional, for tickets saved before redaction was switched on:
   ```
   docker compose exec api python -m app.cli redact-pii            # shows what it would change
   docker compose exec api python -m app.cli redact-pii --apply    # saves the changes
   ```
   The knowledge base keeps the old text of tickets that were already ingested until they're
   ingested again.

No database migration is needed.

---

## 2. PII redaction

Every piece of text that NEXUS stores or sends to an LLM passes through Presidio first:

| Text | Where |
|---|---|
| New ticket title and description | `POST /tickets` |
| Resolution notes and status-change notes | `PATCH /tickets/{id}`, `POST /tickets/{id}/status` |
| Tickets imported from ServiceNow and other systems | integration sync (ServiceNow keeps the original) |
| AI Knowledge Query questions | `/rag/query`, `/rag/query/stream` |
| Documents and tickets added to the knowledge base | `/rag/ingest/*` and the 6-hourly ingest job |

**What's removed:** `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD`, `IBAN_CODE`, `US_SSN`,
`IN_PAN`, `IN_AADHAAR`. Each is replaced by its type, e.g. `Call <PERSON> on <PHONE_NUMBER>`.

**What's kept on purpose:** IP addresses, host names, URLs, dates and times, error codes. The agents
need them to diagnose the ticket. spaCy sometimes takes error codes or host names for people's names
(`ORA-00060`, `api-7f9c`), so a "name" containing a digit, hyphen, dot or slash is never redacted.

Each redaction is recorded in the audit log (`details.pii_redacted`) and in the
`nexus_pii_redactions_total` metric.

Settings (`.env`): `PII_REDACTION_ENABLED` (default `true`), `PII_ENTITIES` (JSON list),
`PII_SPACY_MODEL` (default `en_core_web_sm`; `en_core_web_lg` finds names better but is 560 MB).

**Demo:** create a ticket with *"Priya Sharma (priya@acme.com, +91 98450 12345) can't reach
vpn-gw-02 at 10.1.2.3"*. The saved ticket shows `<PERSON> (<EMAIL_ADDRESS>, <PHONE_NUMBER>) can't
reach vpn-gw-02 at 10.1.2.3`.

---

## 3. Prometheus and Grafana

- The **API** serves metrics at http://localhost:8000/metrics.
- The **Celery worker** runs agents in 4 processes. They share their numbers through
  `PROMETHEUS_MULTIPROC_DIR` and the combined result is served on port 9101 inside Docker.
- **Prometheus** (http://localhost:9090) collects both every 15 seconds and keeps 15 days.
- **Grafana** (http://localhost:3000) opens on the **NEXUS AI** dashboard without signing in.
  Sign in as `admin` with `GRAFANA_ADMIN_PASSWORD` to edit it.

| Metric | Meaning |
|---|---|
| `nexus_tickets_created_total{priority, category, source}` | Tickets created in NEXUS (`source="nexus"`) or imported (`source="servicenow"`) |
| `nexus_agent_runs_total{outcome}` | Agent runs: `resolved`, `needs_human` (paused for review or no resolution), `error` |
| `nexus_agent_run_seconds` | How long an agent run takes |
| `nexus_rag_query_seconds` | How long an AI Knowledge Query takes |
| `nexus_integration_syncs_total{connection, result}` | Integration syncs: `success`, `partial`, `failed`, `skipped` |
| `nexus_integration_tickets_total{connection, outcome}` | Tickets read per sync: `created`, `updated`, `unchanged`, `failed` |
| `nexus_pii_redactions_total{entity}` | Personal-data items removed, by type |
| `nexus_http_requests_total`, `nexus_http_request_duration_seconds` | API traffic per endpoint and status code |

The API's own counters start from zero when the `api` container restarts or reloads; Grafana's
panels use `increase()`, which handles that.

---

## 4. LangSmith trace links

Each agent run now gets a run ID chosen by NEXUS, saved on the ticket **before** the run starts,
so even a run that crashes links to its trace. In LangSmith the run is named
`NEXUS NX-2026-00042 (run)` (or `(resume)` after a human review) and tagged with the ticket
number. On the ticket page, agents and admins see **View in LangSmith** in the Agent Trace panel.

This needs `LANGSMITH_API_KEY` and `LANGCHAIN_TRACING_V2=true` in `.env` (both already set).
`GET /api/v1/agents/langsmith/{ticket_id}` returns the link. Tickets whose agents ran before this
change have no run ID, so no link.

---

## 5. OpenTelemetry and Jaeger

The API sends a trace of every request, with each database query as a child span, to Jaeger.
Open http://localhost:16686, pick service **nexus-api** and click **Find Traces**. Health checks
and `/metrics` aren't traced.

Audit-log rows written during a traced request get its `trace_id` and `span_id`, so an audit
entry can be matched to its request in Jaeger.

To turn tracing off, add `OTEL_ZIPKIN_ENDPOINT=` (empty) to `.env` and run
`docker compose up -d --force-recreate api`.

LangSmith covers the LLM side (prompts, tool calls, tokens); Jaeger covers the HTTP and database
side. Celery tasks aren't traced in Jaeger yet.

---

## 6. Logs: Loki and Alloy

**Alloy** reads the output of every `nexus-*` container through the Docker socket (read-only) and
sends it to **Loki**, which keeps it for 7 days. Each line is labelled with its `container`
(e.g. `nexus-api`, `nexus-celery`, `nexus-postgres`) and compose `service`.

- **Grafana:** the NEXUS AI dashboard's **Logs** row shows error lines per container and the
  latest error lines. For any search, open **Explore**, choose **Loki** and try
  `{container="nexus-celery"} |= "Traceback"`.
- **Security agent:** `scan_system_logs` (in `agents/tools/search_tools.py`) queries Loki for the
  last hour. Before every assessment the agent looks for lines with the ticket's keywords and
  for recent sign-in failures (Keycloak and API). Personal data in the lines is redacted, and
  lines that look like instructions to an AI are withheld, because the result goes into the
  LLM prompt. If Loki is down the agent is told so, not that the logs are clean.

| `log_type` | Containers |
|---|---|
| `application` | api, celery worker, celery beat |
| `database` | postgres, qdrant, redis |
| `auth` | keycloak, api |
| `network`, `system` | all NEXUS containers |

Alloy's own status page is at http://localhost:12345. On Windows, Docker Desktop provides the
socket Alloy reads (`/var/run/docker.sock`); no setting is needed.

These are NEXUS's own logs. To give the agents a client's logs as well, add another Alloy source
(for example a folder of log files) in `monitoring/alloy.alloy`.

---

## 7. Files

| File | What |
|---|---|
| `backend/app/middleware/security.py` | `redact_pii`, `redact_fields`, `redact_documents` |
| `backend/app/middleware/tracing.py` | Metrics, `/metrics` setup, OpenTelemetry setup |
| `backend/app/middleware/worker_metrics.py` | Celery worker metrics endpoint (:9101) |
| `agents/supervisor.py` | LangSmith run IDs (`_run_config`) |
| `backend/app/api/v1/agents.py` | `GET /agents/langsmith/{ticket_id}` |
| `monitoring/prometheus.yml` | What Prometheus collects |
| `monitoring/grafana/` | Grafana data sources (Prometheus, Loki) and the NEXUS AI dashboard |
| `monitoring/loki.yml`, `monitoring/alloy.alloy` | Log store and log collector |
| `agents/tools/search_tools.py` | `scan_system_logs` (Loki query, redaction, injection filter) |
| `backend/tests/test_quick_wins.py` | Tests for all of the above |
