# Starting NEXUS AI after a laptop restart

Use this every time you restart your laptop, or whenever the app isn't loading.
It assumes the one-time setup in [SETUP_GUIDE.md](SETUP_GUIDE.md) is already done.

**Your data is safe across restarts.** Tickets and users (PostgreSQL), the vector index (Qdrant) and
the keyword index (`rag/data/`) are all stored on disk. You don't need to re-seed, re-register or
re-run setup.

Commands are for **PowerShell** in the VS Code terminal, run from the project folder
(`C:\Users\Murali\projects\nexus-ai`).

---

## Quick version

```powershell
# 1. Start Docker Desktop and wait until it says "Engine running"

# 2. Start the backend (safe to run even if it's already up)
docker compose up -d

# 3. Start the frontend in a second terminal, and leave it open
cd frontend
npm run dev

# 4. Open http://localhost:5173 in your browser and log in
```

---

## Step by step

### 1. Start Docker Desktop

Open **Docker Desktop** from the Start menu. Wait until the bottom-left corner shows
**Engine running** (usually 30–60 seconds).

> Tip: in Docker Desktop, open **Settings → General** and tick **Start Docker Desktop when you sign in**.
> All NEXUS containers use `restart: unless-stopped`, so with this on, the whole backend comes back
> by itself after a reboot. The only exception is if you stopped the containers yourself before the reboot.

### 2. Start the backend

```powershell
docker compose up -d
```

This starts any containers that aren't running yet: postgres, redis, qdrant, ollama, keycloak,
servicenow-mock, api, celery-worker and celery-beat. (The mock ServiceNow is at http://localhost:8090;
see [INTEGRATIONS.md](INTEGRATIONS.md).) It's safe to run even if they're already running.

Check that everything is up (give it about a minute after Docker starts):

```powershell
docker compose ps
```

`nexus-postgres`, `nexus-redis`, `nexus-api` and `nexus-celery` should show **(healthy)**; the
others should show **Up**.

Then confirm the API can reach its database and Redis. Open this in your browser:

- http://localhost:8000/api/v1/health/ready → should show `"status":"ready"` with `"postgres":"ok"` and `"redis":"ok"`

### 3. Start the frontend

The frontend is **not** in Docker, so it never restarts on its own. Open a **second terminal**
(Terminal → New Terminal) and run:

```powershell
cd frontend
npm run dev
```

Wait for the line `Local: http://localhost:5173/`. **Leave this terminal open**: closing it stops the frontend.

### 4. Open the app

Go to **http://localhost:5173** and log in with your username and password.

---

## Only when needed

| Situation | Command |
|---|---|
| You pulled or wrote code that adds a database migration | `docker compose exec api alembic upgrade head` (safe to run any time) |
| `package.json` changed, or `frontend/node_modules` is missing | `cd frontend` then `npm install` (before `npm run dev`) |
| You changed agent or worker code (`agents/`, `rag/`, `backend/app/workers/`) | `docker compose restart celery-worker` (the `api` container reloads by itself) |
| Changed something in `.env` | `docker compose up -d --force-recreate` |
| Need a new admin account | Register it on the API, then `docker compose exec api python -m app.cli make-admin <username>` |
| Knowledge base is empty (AI answers say "no relevant documents") | `docker compose exec api python -m rag.seed` |

---

## Shutting down

```powershell
# In the frontend terminal
Ctrl + C

# Stop the backend but keep all data
docker compose stop
```

> **Do not run `docker compose down -v`.** The `-v` deletes the database and vector volumes, which
> means all tickets, users and the knowledge base are gone. Plain `docker compose down` is fine;
> it removes the containers but keeps the data.

---

## If something doesn't work

| Symptom | Likely cause | Fix |
|---|---|---|
| `error during connect` / `cannot find the file specified` when running `docker` | Docker Desktop isn't running yet | Start it and wait for **Engine running** |
| http://localhost:5173 says "can't reach this page" | Frontend isn't running | Step 3: `cd frontend`, `npm run dev` |
| Vite prints `Port 5173 is in use, trying another one...` | An old dev server is still running | Use the URL Vite prints (e.g. `:5174`), or close the old terminal and start again |
| Page loads but login fails or shows a network error | Backend not ready | `docker compose ps`; if `nexus-api` isn't healthy: `docker compose logs api --tail 50` |
| Login says invalid credentials | Wrong user/password, or the account was never created | Register again; see the table above for admin |
| `/health/ready` reports the database or Redis as down | Those containers are still starting | Wait 30 s and retry; else `docker compose restart postgres redis` |
| First start after a reboot is slow | Ollama re-checks its embedding model on start | Wait a minute; `docker compose logs ollama` |
| Agent runs never finish | Celery worker stopped or unhealthy | `docker compose logs celery-worker --tail 50`, then `docker compose restart celery-worker` |

---

## Shortcuts in VS Code

**Terminal → Run Task…** has ready-made tasks for this project:

- **🐳 Docker: Start All Services**, which runs `docker compose up -d`
- **⚛️ Frontend: Install & Start**, which runs `npm install` and `npm run dev`
- **🐳 Docker: View Logs (API)**, which follows the API logs

---

## Addresses

| What | URL |
|---|---|
| **App (frontend)** | http://localhost:5173 |
| API docs (Swagger) | http://localhost:8000/docs |
| API health | http://localhost:8000/api/v1/health/ready |
| Keycloak admin | http://localhost:8080 |
| Qdrant dashboard | http://localhost:6333/dashboard |
| PostgreSQL (for DB tools) | `localhost:15432` |
