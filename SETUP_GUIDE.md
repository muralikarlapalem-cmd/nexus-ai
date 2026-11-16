# NEXUS AI — Complete VS Code Setup Guide

> **All 6 phases** · Python 3.12 · Docker · React · LangGraph · QLoRA

---

## Prerequisites

Install these before starting:

| Tool | Version | Download |
|------|---------|----------|
| Docker Desktop | Latest | https://docker.com |
| Python | 3.12+ | https://python.org |
| Node.js | 20 LTS | https://nodejs.org |
| VS Code | Latest | https://code.visualstudio.com |
| Git | Latest | https://git-scm.com |

**VS Code Extensions** — install all of these:
```
Python (Microsoft)          ms-python.python
Pylance                     ms-python.vscode-pylance
Docker                      ms-azuretools.vscode-docker
ES7+ React/Redux Snippets   dsznajder.es7-react-js-snippets
Tailwind CSS IntelliSense   bradlc.vscode-tailwindcss
GitLens                     eamodio.gitlens
Thunder Client              rangav.vscode-thunder-client
YAML                        redhat.vscode-yaml
Even Better TOML            tamasfe.even-better-toml
```

Install all at once from terminal:
```bash
code --install-extension ms-python.python ms-python.vscode-pylance \
  ms-azuretools.vscode-docker dsznajder.es7-react-js-snippets \
  bradlc.vscode-tailwindcss eamodio.gitlens rangav.vscode-thunder-client \
  redhat.vscode-yaml tamasfe.even-better-toml
```

---

## Step 1 — Download & Open

```bash
# 1. Unzip the project
unzip nexus-ai-complete.zip
cd nexus-ai

# 2. Open in VS Code
code .
```

VS Code will detect the workspace. Accept any recommended extension installs.

---

## Step 2 — Python Virtual Environment

```bash
cd backend
python -m venv .venv

# Activate (choose your OS):
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows PowerShell
.venv\Scripts\activate.bat         # Windows CMD
```

Configure VS Code to use this interpreter:
- Press `Cmd+Shift+P` → "Python: Select Interpreter"
- Choose `./backend/.venv/bin/python`

Install all dependencies:
```bash
pip install -r requirements.txt
# This installs all 6 phases' dependencies
```

> ⏱ First install takes 5–8 minutes (sentence-transformers is large)

---

## Step 3 — Environment Variables

```bash
# From project root:
cp .env.example .env
```

Open `.env` and fill in the required values:

```env
# Minimum required for Phase 1+2 (local dev):
POSTGRES_PASSWORD=nexus_secret
REDIS_PASSWORD=redis_secret
JWT_SECRET_KEY=your-32-char-secret-here-make-it-random

# Required for Phase 3+ (agents + RAG generation):
OPENAI_API_KEY=sk-your-openai-key-here

# Optional: web search for tickets the knowledge base can't answer (https://tavily.com)
TAVILY_API_KEY=tvly-your-key-here

# Optional (Phase 5 tracing):
LANGSMITH_API_KEY=ls__your-key-here
LANGCHAIN_TRACING_V2=true
```

> Get your OpenAI API key at: https://platform.openai.com/api-keys

---

## Step 4 — Start Docker Services

Make sure Docker Desktop is running, then:

```bash
# From project root:
docker compose up -d

# Verify all services are healthy:
docker compose ps
```

Expected output — all services should show `healthy` or `running`:
```
nexus-postgres   healthy
nexus-redis      healthy
nexus-qdrant     healthy
nexus-ollama     running   ← pulls nomic-embed-text on first start (~3 min)
nexus-keycloak   running
nexus-api        running
nexus-celery     running
nexus-beat       running
```

Check logs if anything fails:
```bash
docker compose logs api          # FastAPI logs
docker compose logs celery-worker
docker compose logs qdrant
```

---

## Step 5 — Database Migrations (Phase 1)

```bash
cd backend
alembic upgrade head
```

Expected output:
```
INFO  [alembic.runtime.migration] Running upgrade -> abc123, create users table
INFO  [alembic.runtime.migration] Running upgrade abc123 -> def456, create tickets table
INFO  [alembic.runtime.migration] Running upgrade def456 -> ghi789, create audit_logs table
```

---

## Step 6 — Verify the API (Phase 1)

Open http://localhost:8000/docs — you should see the Swagger UI.

**Register your first user:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@nexus.ai",
    "username": "admin",
    "password": "Admin1234!"
  }'
```

**Make that user an admin.** Registration always creates a `user` account (a `role` field is rejected),
so the first admin is promoted from the server shell:
```bash
docker compose exec api python -m app.cli make-admin admin
```
After that, the admin can change other users' roles with `PATCH /api/v1/users/{id}/role`.

**Login and get a token:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"Admin1234!"}' | python -m json.tool
```

Copy the `access_token` — you'll need it for all subsequent calls.

---

## Step 7 — Seed the Knowledge Base (Phase 2)

```bash
cd backend
python -m rag.seed
```

Expected output:
```
INFO  Loaded Markdown: network_runbook.md → 8 elements
INFO  Loaded Markdown: database_runbook.md → 10 elements
INFO  Embedding 18 chunks...
INFO  Upserted 18 vectors into 'nexus_knowledge'
INFO  BM25 index built: 18 documents
INFO  Seed complete
```

**Test RAG query:**
```bash
TOKEN="your-access-token-here"
curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "How do I fix PostgreSQL max connections error?"}'
```

---

## Step 8 — Create and Run Your First Ticket (Phase 3)

**Create a ticket:**
```bash
curl -X POST http://localhost:8000/api/v1/tickets/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Production database unreachable",
    "description": "Application cannot connect to PostgreSQL. Getting FATAL: remaining connection slots are reserved. This started after todays deploy.",
    "category": "database",
    "priority": "high"
  }'
```

Copy the `id` from the response.

**Trigger the agent pipeline:**
```bash
TICKET_ID="paste-ticket-uuid-here"
curl -X POST http://localhost:8000/api/v1/agents/run/$TICKET_ID \
  -H "Authorization: Bearer $TOKEN"
```

**Poll for status:**
```bash
curl http://localhost:8000/api/v1/agents/status/$TICKET_ID \
  -H "Authorization: Bearer $TOKEN"
```

---

## Step 9 — Start the Frontend (Phase 4)

Open a **new terminal** (keep backend running):
```bash
cd frontend
npm install          # first time: ~2 minutes
npm run dev
```

Open http://localhost:5173 in your browser.

Login with the admin account you created in Step 6.

---

## Step 10 — Run Evaluations (Phase 5)

```bash
cd backend
# Make sure at least 3-5 RAG queries have been answered first (builds eval context)

# RAG quality evals:
python -m evals.rag_evals

# Agent pipeline evals:
python -m evals.agent_evals
```

Results saved to `evals/results/`.

---

## Step 11 — Fine-Tuning (Phase 6)

**Export training data:**
```bash
cd backend
python -m fine_tuning.generate_dataset \
  --output fine_tuning/data/nexus_train.jsonl
```

**Open the Colab notebook:**
1. Go to https://colab.research.google.com
2. File → Upload notebook → select `fine_tuning/nexus_finetune.ipynb`
3. Runtime → Change runtime type → T4 GPU
4. Upload your `nexus_train.jsonl` to `/content/`
5. Run all cells in order

**Deploy the fine-tuned model:**
```bash
# After downloading the GGUF from Colab:
chmod +x fine_tuning/deploy_ollama.sh
./fine_tuning/deploy_ollama.sh ./fine_tuning/nexus-finetuned-Q4_K_M.gguf
```

---

## Service URLs

| Service | URL | Credentials |
|---------|-----|-------------|
| FastAPI Docs | http://localhost:8000/docs | — |
| React Frontend | http://localhost:5173 | admin / Admin1234! |
| Qdrant Dashboard | http://localhost:6333/dashboard | — |
| Keycloak Admin | http://localhost:8080 | admin / admin_secret |
| Ollama | http://localhost:11434 | — |

---

## Useful VS Code Tasks

Add `.vscode/tasks.json` to run common commands from the VS Code Command Palette:
```json
{
  "version": "2.0.0",
  "tasks": [
    { "label": "Docker: Start All",   "type": "shell", "command": "docker compose up -d" },
    { "label": "Docker: Stop All",    "type": "shell", "command": "docker compose down" },
    { "label": "Backend: Dev Server", "type": "shell", "command": "cd backend && uvicorn app.main:app --reload" },
    { "label": "Frontend: Dev",       "type": "shell", "command": "cd frontend && npm run dev" },
    { "label": "Run Tests",           "type": "shell", "command": "cd backend && pytest tests/ -v" },
    { "label": "Run RAG Evals",       "type": "shell", "command": "cd backend && python -m evals.rag_evals" },
    { "label": "Seed Knowledge Base", "type": "shell", "command": "cd backend && python -m rag.seed" },
    { "label": "DB Migration",        "type": "shell", "command": "cd backend && alembic upgrade head" }
  ]
}
```

`Cmd+Shift+P` → "Tasks: Run Task" to access them.

---

## Troubleshooting

**Ollama slow to start?** It pulls `nomic-embed-text` (~270MB) on first run. Wait 3–5 minutes.

**Qdrant connection refused?** Check `docker compose ps qdrant`. The health check takes 15s.

**ImportError on rag module?** The `rag/` folder must be on the Python path. Run from `backend/` dir with `PYTHONPATH=..` or use the Docker setup which mounts it.

**LangGraph agents timeout?** Ensure `OPENAI_API_KEY` is set in `.env` and the account has quota.

**Frontend can't reach API?** The Vite proxy (`/api → localhost:8000`) requires the FastAPI server to be running on port 8000.
