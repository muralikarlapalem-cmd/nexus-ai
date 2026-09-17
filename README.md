# 🧠 NEXUS AI — Multi-Agent IT Service Intelligence Platform

> End-to-end Python GenAI Engineering showcase: FastAPI · LangGraph · RAG · LangSmith · RAGAS · QLoRA

---

## Architecture

```
React Frontend  →  FastAPI Backend  →  LangGraph Supervisor
                                             ↓
                          ┌──────────────────────────────────┐
                          │  Triage · Diagnosis · Knowledge  │
                          │  Security · Analytics Agents     │
                          └──────────────────────────────────┘
                                             ↓
                          Qdrant (RAG)  ·  LangSmith (Traces)
                          RAGAS (Evals) ·  QLoRA (Fine-Tune)
```

## Build Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Foundation: FastAPI + Auth + DB | ✅ Complete |
| 2 | RAG Pipeline: Qdrant + LangChain | 🔜 Next |
| 3 | Agents + LangGraph Orchestration | 🔜 |
| 4 | React Frontend Dashboard | 🔜 |
| 5 | Tracing + Evals + Security | 🔜 |
| 6 | Fine-Tuning Pipeline (QLoRA) | 🔜 |

---

## Phase 1: Quick Start

### Prerequisites
- Docker + Docker Compose
- Python 3.12

### 1. Clone & configure
```bash
git clone https://github.com/your-username/nexus-ai
cd nexus-ai
cp .env.example .env
# Edit .env with your values
```

### 2. Start all services
```bash
docker compose up -d
```

Services started:
- PostgreSQL → `localhost:5432`
- Redis → `localhost:6379`
- Keycloak → `http://localhost:8080`
- FastAPI → `http://localhost:8000`
- Celery Worker + Beat

### 3. Apply DB migrations
```bash
cd backend
alembic upgrade head
```

### 4. Test the API
```bash
# API docs
open http://localhost:8000/docs

# Register a dev user
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@nexus.ai","username":"admin","password":"Admin1234!","role":"admin"}'

# Login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"Admin1234!"}'

# Create a ticket
curl -X POST http://localhost:8000/api/v1/tickets/ \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"title":"Server down","description":"Production server is unreachable","priority":"critical","category":"network"}'
```

### 5. Run tests
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
│   │   ├── main.py              # FastAPI app
│   │   ├── config.py            # Pydantic settings
│   │   ├── database.py          # Async SQLAlchemy
│   │   ├── models/              # ORM: User, Ticket, AuditLog
│   │   ├── schemas/             # Pydantic v2 schemas
│   │   ├── api/v1/              # Routes: auth, tickets, health
│   │   ├── core/                # security.py, deps.py (RBAC)
│   │   └── workers/             # Celery tasks
│   ├── alembic/                 # DB migrations
│   ├── tests/
│   ├── Dockerfile
│   └── requirements.txt
├── agents/                      # Phase 3: LangGraph
├── rag/                         # Phase 2: Qdrant + LangChain
├── evals/                       # Phase 5: RAGAS + DeepEval
├── fine_tuning/                 # Phase 6: QLoRA
├── frontend/                    # Phase 4: React
├── docker-compose.yml
└── .env.example
```

## API Endpoints (Phase 1)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/health` | None | Liveness probe |
| GET | `/api/v1/health/ready` | None | Readiness (DB+Redis) |
| POST | `/api/v1/auth/register` | None (dev) | Register local user |
| POST | `/api/v1/auth/login` | None (dev) | Local login → JWT |
| GET | `/api/v1/auth/me` | Bearer | Current user profile |
| POST | `/api/v1/tickets/` | Bearer | Create ticket |
| GET | `/api/v1/tickets/` | Bearer | List + paginate tickets |
| GET | `/api/v1/tickets/{id}` | Bearer | Get single ticket |
| PATCH | `/api/v1/tickets/{id}` | Agent/Admin | Update ticket |
| POST | `/api/v1/tickets/{id}/status` | Agent/Admin | Status transition (validated, audit-logged) |
| GET | `/api/v1/users/` | Admin | List all users |
| GET | `/api/v1/users/me` | Bearer | Current user's own profile |
| PATCH | `/api/v1/users/{id}/role` | Admin | Change a user's role |


### Roles & Ticket Status Rules

Four role levels: `admin`, `agent`, `analyst`, `user`. New users default to `user` — promote via `PATCH /api/v1/users/{id}/role` (admin-only).

Ticket status follows a fixed lifecycle: `open → triaged → in_progress → (awaiting_user) → resolved → closed`, or `cancelled` from most non-terminal states. Notably, **a resolved ticket cannot be closed** — `resolved`, `closed`, and `cancelled` are all terminal. Every status change is recorded in `ticket_status_history` with who made the change and why.

---

## Phase 2: RAG Pipeline

### New services
- **Qdrant** → `http://localhost:6333` (vector DB + REST UI)
- **Ollama** → `http://localhost:11434` (local embeddings, no API key needed)

### Quick start for Phase 2
```bash
# Start all services including Qdrant + Ollama
docker compose up -d

# Seed the knowledge base with sample runbooks
cd backend
python -m rag.seed

# Test RAG query via API
curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"query": "How do I fix PostgreSQL connection pool exhaustion?"}'

# Ingest resolved tickets into RAG
curl -X POST "http://localhost:8000/api/v1/rag/ingest/tickets" \
  -H "Authorization: Bearer <token>"
```

### Phase 2 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/rag/ingest/text` | Ingest raw text |
| POST | `/api/v1/rag/ingest/file` | Upload PDF/Markdown file |
| POST | `/api/v1/rag/ingest/tickets` | Ingest resolved tickets |
| POST | `/api/v1/rag/search` | Hybrid search (no generation) |
| POST | `/api/v1/rag/query` | Full RAG: retrieve + generate |
| GET  | `/api/v1/rag/query/stream` | Streaming SSE RAG |
| GET  | `/api/v1/rag/stats` | Qdrant + BM25 stats |

### RAG Architecture
```
Query
  ↓
embed_query (Ollama nomic-embed-text)
  ↓              ↓
Dense ANN      BM25 keyword
(Qdrant)       (rank_bm25)
  ↓              ↓
    RRF Fusion (top-20 → top-10)
        ↓
    Cross-Encoder Rerank (top-3)
        ↓
    Context Formatting
        ↓
    Claude gpt-4o → Answer + Sources
```
