# 🧠 NEXUS AI — Multi-Agent IT Service Intelligence Platform

> End-to-end Python GenAI Engineering showcase: FastAPI · LangGraph · RAG · LangSmith · RAGAS · QLoRA

NEXUS AI is an IT service desk where LLM agents work tickets. A user files a ticket; a LangGraph
supervisor runs it through triage → (security) → diagnosis → knowledge agents, pausing for a human
when the risk is high. Agents ground their answers in a hybrid-search knowledge base built from
runbooks and previously resolved tickets, and every confident resolution is written back as a new
runbook, so the knowledge base grows with use.

---

## Architecture

```
React Frontend (Vite, :5173)
        │  /api proxy
        ▼
FastAPI Backend (:8000) ──► PostgreSQL (users, tickets, status history, audit log)
        │
        │ POST /agents/run/{id}  → Celery task (queue: agents)
        ▼
LangGraph Supervisor  (state checkpointed in Redis)
        │
        ├─ Triage Agent ─────┬─► Security Agent ─┐
        │                    └───────────────────┴─► Diagnosis Agent ─► Knowledge Agent ─► END
        │                                                   │
        └──────────── Human Review (HITL interrupt) ◄───────┘   (any agent can escalate)
        │
        ▼
RAG pipeline: Qdrant (dense) + BM25 (keyword) → RRF fusion → cross-encoder rerank → LLM
```

LLM calls use **OpenAI** via `langchain_openai.ChatOpenAI` (default model `gpt-4o`, set with `LLM_MODEL`).

## Build Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Foundation: FastAPI + Auth + DB | ✅ Complete |
| 2 | RAG Pipeline: Qdrant + BM25 + reranker | ✅ Implemented |
| 3 | Agents + LangGraph Orchestration | ✅ Implemented (triage, security, diagnosis, knowledge, HITL) |
| 4 | React Frontend Dashboard | ✅ Implemented |
| 5 | Tracing + Evals + Security | 🟡 Partial: RAGAS and triage evals exist; scores below CI thresholds; `run_eval_suite` Celery task is a stub; PII detection is regex-only |
| 6 | Fine-Tuning Pipeline (QLoRA) | 🟡 Scaffolded: dataset export, notebook, Ollama deploy script |

**Not built yet:** the Analytics agent / `analytics` router (commented out in `backend/app/main.py`),
and the `index_ticket_for_rag` / `run_eval_suite` Celery tasks (they only log). Ticket indexing works
through `app/workers/rag_tasks.py` instead.

---

## Quick Start

> Already set up and just restarted your laptop? See **[STARTUP.md](STARTUP.md)** for the short
> start-up checklist.

### Prerequisites
- Docker + Docker Compose
- Python 3.12
- Node.js (for the frontend)
- An OpenAI API key (used for generation, and for embeddings by default)

### 1. Clone & configure
```bash
git clone https://github.com/your-username/nexus-ai
cd nexus-ai
cp .env.example .env
# Edit .env: at minimum set the passwords, JWT_SECRET_KEY and OPENAI_API_KEY
# Optional: TAVILY_API_KEY enables web search when the knowledge base has no answer
```

### 2. Start all services
```bash
docker compose up -d
```

| Service | Host address |
|---------|--------------|
| PostgreSQL | `localhost:15432` (container port 5432) |
| Redis (redis-stack-server) | `localhost:6379` |
| Keycloak | `http://localhost:8080` |
| Qdrant | `http://localhost:6333` (REST + UI), `6334` (gRPC) |
| Ollama | `http://localhost:11434` (pulls `nomic-embed-text` on first start) |
| FastAPI | `http://localhost:8000` (docs at `/docs`) |
| Celery worker | queues `default, agents, rag, evals` |
| Celery beat | periodic jobs, see below |

### 3. Apply DB migrations
```bash
docker compose exec api alembic upgrade head
```

### 4. Seed the knowledge base
```bash
docker compose exec api python -m rag.seed
```

### 5. Run the frontend
```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to :8000
```

### 6. Try the API
```bash
# Register a dev user
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@nexus.ai","username":"admin","password":"Admin1234!"}'

# Registration always creates a 'user'; promote the first admin from the server shell
docker compose exec api python -m app.cli make-admin admin

# Login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"Admin1234!"}'

# Create a ticket
curl -X POST http://localhost:8000/api/v1/tickets/ \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"title":"Server down","description":"Production server is unreachable","priority":"critical","category":"network"}'

# Run the agent pipeline on it
curl -X POST http://localhost:8000/api/v1/agents/run/<ticket_id> \
  -H "Authorization: Bearer <access_token>"

# Ask the knowledge base directly
curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"query": "How do I fix PostgreSQL connection pool exhaustion?"}'
```

### 7. Run tests
```bash
cd backend
pytest tests/ -v --asyncio-mode=auto
```

---

## Project Structure

```
nexus-ai/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app + router registration
│   │   ├── config.py            # Pydantic settings
│   │   ├── database.py          # Async SQLAlchemy
│   │   ├── models/              # ORM: User, Ticket, AuditLog
│   │   ├── schemas/             # Pydantic v2 schemas
│   │   ├── api/v1/              # Routes: auth, users, tickets, rag, agents, health
│   │   ├── core/                # security.py, deps.py (RBAC)
│   │   ├── middleware/          # security headers, request tracing
│   │   └── workers/             # celery_app.py, rag_tasks.py
│   ├── alembic/                 # DB migrations
│   ├── tests/
│   ├── Dockerfile
│   └── requirements.txt
├── agents/                      # LangGraph supervisor, agents, tools
├── rag/                         # Ingestion (loaders, chunker, embedder) + retrieval
├── evals/                       # RAGAS RAG evals + triage agent evals
├── fine_tuning/                 # Dataset export, QLoRA notebook, Ollama deploy
├── frontend/                    # React + Vite + Tailwind
├── docker-compose.yml
└── .env.example
```

---

## API Endpoints

All paths are prefixed with `/api/v1`.

### Health, Auth, Users

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health/` | None | Liveness probe |
| GET | `/health/ready` | None | Readiness (DB + Redis) |
| GET | `/health/info` | None | App info |
| POST | `/auth/register` | None (dev) | Register local user |
| POST | `/auth/login` | None (dev) | Local login → JWT |
| POST | `/auth/refresh` | Refresh token | New access token |
| GET | `/auth/me` | Bearer | Current user profile |
| GET | `/auth/keycloak-url` | None | Keycloak login URL |
| GET | `/users/` | Admin | List all users |
| GET | `/users/me` | Bearer | Current user's own profile |
| PATCH | `/users/{id}/role` | Admin | Change a user's role |

### Tickets

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/tickets/` | Bearer | Create ticket |
| GET | `/tickets/` | Bearer | List + paginate tickets |
| GET | `/tickets/{id}` | Bearer | Get single ticket |
| PATCH | `/tickets/{id}` | Agent/Admin | Update ticket |
| POST | `/tickets/{id}/status` | Agent/Admin | Status transition (validated, audit-logged) |

### RAG

| Method | Path | Description |
|--------|------|-------------|
| POST | `/rag/ingest/text` | Ingest raw text |
| POST | `/rag/ingest/file` | Upload PDF/Markdown file |
| POST | `/rag/ingest/tickets` | Ingest resolved tickets |
| POST | `/rag/search` | Hybrid search (no generation) |
| POST | `/rag/query` | Full RAG: retrieve + generate |
| GET  | `/rag/query/stream` | Streaming SSE RAG |
| GET  | `/rag/stats` | Qdrant + BM25 stats |
| DELETE | `/rag/collection` | Delete the collection |

### Agents

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/agents/run/{ticket_id}` | Agent/Admin | Dispatch the supervisor via Celery |
| GET | `/agents/status/{ticket_id}` | Bearer | Ticket status + agent outputs so far |
| POST | `/agents/resume/{ticket_id}` | Agent/Admin | Resume after human review: `{"approved": bool, "next_agent": str, "notes": str}`. Saves the outcome to the ticket; returns `"paused"` if the run stopped for review again, 409 if nothing is paused |
| GET | `/agents/trace/{ticket_id}` | Bearer | Agent message trace, current node, iteration count |

### Roles & Ticket Status Rules

Four role levels: `admin`, `agent`, `analyst`, `user`. Self-registration always creates a `user`
(requests that include a `role` are rejected with 422). Promote users via `PATCH /api/v1/users/{id}/role`
(admin-only); bootstrap the first admin with `python -m app.cli make-admin <username>` in the api container.

Ticket status follows a fixed lifecycle: `open → triaged → in_progress → (awaiting_user) → resolved → closed`,
or `cancelled` from most non-terminal states. Notably, **a resolved ticket cannot be closed**:
`resolved`, `closed`, and `cancelled` are all terminal. Every status change is recorded in
`ticket_status_history` with who made the change and why.

---

## Agent Pipeline

`agents/supervisor.py` builds a LangGraph `StateGraph` over a shared `TicketState`. Each node sets
`state["next"]` and one routing function sends the run to that node. State is checkpointed in Redis
(`AsyncRedisSaver`, thread id = ticket id), so a paused run can be resumed later.

| Node | Tools | Output → next |
|------|-------|---------------|
| `triage_agent` | `check_sla_status` | Category, validated priority, keywords, affected components, confidence. Next is `security_agent` if security keywords are present, else `diagnosis_agent`; `human_review` on data-loss risk, outage > 1h, breach, or confidence < 0.4 |
| `security_agent` | `cve_lookup` (NVD API), `rag_search`, regex PII detection | Threat level, CVEs, PII types, remediation. Next is `human_review` if critical / escalate / containment, else `diagnosis_agent` |
| `diagnosis_agent` | knowledge base search, Tavily web search (only when the knowledge base has no good match: no results, a search error, or best reranker score below `KB_MIN_RELEVANCE`, default 0), `check_sla_status` | Root cause, numbered resolution steps with commands, verification steps, and the sources used (`knowledge_sources`, `web_sources` with URLs, `answer_basis`); the resolution notes end with a **Sources** list. Next is `human_review` if escalation is needed, else `knowledge_agent` |
| `knowledge_agent` | `generate_runbook`, `update_ticket_field` | If diagnosis confidence ≥ 0.6, generates a runbook and ingests it into RAG; writes resolution to the ticket. Next: `END` |
| `human_review` | — | Pauses with `interrupt()`. On resume: continues to `next_agent` if approved, else `END` |
| `guard` | — | Forces `END` after 8 iterations |

Whenever a run stops, whether it is the first run (`run_supervisor()`) or a resume after human review
(`resume_supervisor()`), `persist_final_state()` saves the triage output, diagnosis output and resolution
notes on the ticket, then:

| How the run ended | Ticket |
|---|---|
| Finished with a resolution | `resolved`, `resolved_at` set |
| Paused for human review | stays `in_progress`, assigned to `human` |
| Rejected by the reviewer | stays `in_progress`, assigned to `human` |
| Stopped by the guard (error) | stays `in_progress`, assigned to `human` |

---

## RAG Pipeline

### Ingestion
Documents → chunk (512 tokens, 64 overlap) → embed → upsert into Qdrant (tagged with `tenant_id`) → add to BM25 index.

**Embeddings:** the default is **OpenAI `text-embedding-3-small`** (1536-dim). For free local
embeddings set `EMBEDDING_PROVIDER=ollama` and `VECTOR_DIM=768` to use `nomic-embed-text`. Changing
the provider requires re-creating the Qdrant collection, because the vector size changes.

### Query
```
Query
  ↓
embed query
  ↓                     ↓
Dense ANN (Qdrant)    BM25 keyword (rank_bm25)
top 10                top 10
  ↓                     ↓
  Reciprocal Rank Fusion (k = 60) → up to 20 candidates
          ↓
  Cross-encoder rerank (ms-marco-MiniLM-L-6-v2) → top 3
          ↓
  Context formatting with source citations
          ↓
  OpenAI chat model (default gpt-4o) → answer + sources
```

All of these numbers are settings in `rag/config.py` (`dense_top_k`, `sparse_top_k`, `rrf_k`,
`rerank_candidates`, `rerank_top_k`) and can be overridden in `.env`.

---

## Background Jobs (Celery Beat)

| Schedule | Task |
|----------|------|
| Every 15 min | `check_sla_breaches`: flag open tickets past their SLA deadline |
| Every 6 h | `ingest_resolved_tickets_task`: add newly resolved tickets to RAG |
| Every 7 days | `rebuild_bm25_task`: rebuild the BM25 index from Qdrant |

---

## Evals

```bash
python -m evals.rag_evals --ci      # RAGAS; exits 1 if below thresholds
python -m evals.agent_evals         # triage accuracy; CI gate is ≥ 70%
```

RAG thresholds: faithfulness ≥ 0.75, answer relevancy ≥ 0.70, context recall ≥ 0.65, context precision ≥ 0.60.
Results are written to `evals/results/`; the most recent run there scored below these thresholds.

---

## Frontend

React + Vite + Tailwind on `http://localhost:5173`. Pages: Login, Dashboard, Tickets, Ticket Detail,
Query (ask the knowledge base), Agent Monitor.
