# Integrations: ServiceNow and other ticketing systems

NEXUS can now take tickets from an external ticketing system, run its agents on
them, and post the result back. It ships with:

- a **ServiceNow connector** (Table API: incidents in, work notes out)
- a **Generic REST connector** you set up with field paths instead of code
- a **mock ServiceNow** container, so you can try everything without a real instance
- an **Integrations page** (Test, Sync now, sync history, settings)

```
 ServiceNow ──┐                                              ┌─► Triage → Diagnosis → Knowledge
 (or mock)    │                                              │     (existing agents, unchanged)
 Jira / any ──┼─► Connector ─► ExternalTicket ─► Sync engine ┤
 REST API     │   translates    one common        upsert,    │
              │   both ways     format            dedupe     └─► work note posted back
 push ────────┘ (webhook)                                         to the source ticket
```

Everything below "Connector" is shared, so a new source system is one small
file (or no code at all, using the Generic REST connector).

---

## 1. Set up (about 10 minutes)

All commands are PowerShell in the VS Code terminal, from the project folder.

### 1.1 Add three settings to `.env`

```powershell
# Pick any values; these are only for the local mock
INTEGRATION_SERVICENOW_MOCK_PASSWORD=choose-a-password
INTEGRATION_SERVICENOW_MOCK_WEBHOOK_SECRET=choose-a-long-random-string
PUBLIC_APP_URL=http://localhost:5173
```

A connection never stores a password. It stores the **name** of an
environment variable, and the name must start with `INTEGRATION_`. That rule
stops anyone from pointing a connection at their own server with
`OPENAI_API_KEY` as the "password".

### 1.2 Start the mock and restart the backend

```powershell
docker compose up -d --build servicenow-mock
docker compose restart api celery-worker celery-beat
```

The worker and beat must restart to load the new sync tasks. Check the mock:
open <http://localhost:8090>. You'll see three demo incidents.

### 1.3 Database

In development mode the API creates the three new tables at startup, so they
already exist. To keep Alembic in step (needed for production), check where
your database is:

```powershell
docker compose exec api alembic current
```

| It shows | Run |
|---|---|
| `a1b2c3d4e5f6` | `docker compose exec api alembic upgrade head` |
| nothing (Alembic never ran) | `docker compose exec api alembic stamp a1b2c3d4e5f6` then `docker compose exec api alembic upgrade head` |
| `b7c8d9e0f1a2 (head)` | nothing to do |

The new migration skips tables that already exist, so it's safe either way.
(`alembic/env.py` on `main` had a missing quote that stopped every Alembic
command; it's fixed on this branch.)

### 1.4 Create the connection

```powershell
docker compose exec api python -m app.integrations.cli setup-mock
```

This creates **ServiceNow (mock)**, checking for changes every 2 minutes.
You can also add it by hand on the Integrations page.

### 1.5 Optional: make your existing tickets "come from ServiceNow"

```powershell
# See what would happen
docker compose exec api python -m app.integrations.cli link-existing --dry-run
# Do it
docker compose exec api python -m app.integrations.cli link-existing
```

This copies every NEXUS ticket that has no source yet into the mock as an
incident (same dates, priority, status and resolution) and links the two. Your
tickets keep their NX numbers and gain an INC number and a link. Nothing in
the tickets table changes, and no agents run.

To undo it, delete the links (the tickets stay):

```powershell
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "DELETE FROM external_ticket_links WHERE source_name = 'ServiceNow (mock)';"
```

### 1.6 First sync

Open **Integrations** in NEXUS → **Test** → **Sync now** → expand **Sync history**.

The first sync imports the three demo incidents and queues the agents for
them (three OpenAI runs). Linked tickets from 1.5 show as "Same".

---

## 2. Demo flow (what to show a client)

1. Open the mock at <http://localhost:8090> and create an incident, e.g.
   "ORA-00060 deadlock detected in order service", Impact 1, Urgency 2.
2. Within 2 minutes (or instantly with push mode, below) it appears in
   NEXUS **Tickets** with a green `ServiceNow · INC00100xx` badge, and the
   agents start.
3. When the agents finish, open the incident in the mock again (or click the
   badge): **Activity** shows a work note from NEXUS with the triage, likely
   root cause, suggested steps with commands, and verification steps.
4. Change the incident's state to Resolved in the mock. On the next sync the
   NEXUS ticket becomes resolved too.

Already-run tickets: open the ticket in NEXUS and click
**Post result to ServiceNow (mock)**.

### Push mode (instant instead of every 2 minutes)

1. Copy the connection's ID from the push URL shown on its card.
2. Add to `.env`:
   ```
   MOCK_SN_WEBHOOK_URL=http://api:8000/api/v1/integrations/<connection-id>/webhook
   ```
3. `docker compose up -d servicenow-mock`

The mock now calls NEXUS whenever an incident is created or changed, with the
`X-Nexus-Webhook-Token` header. Polling keeps running as a safety net.

---

## 3. Connect a real ServiceNow (free developer instance)

1. Sign up at <https://developer.servicenow.com> and request a Personal
   Developer Instance. Note its URL (`https://devNNNNN.service-now.com`) and
   admin password.
2. In the instance, create an integration user (**User Administration → Users → New**):
   - User ID `nexus.integration`, **Web service access only** ticked
   - **Time zone: GMT** (the "changed since" filter reads dates in the user's time zone)
   - Role: `itil` (read and update incidents)
   - Set a password
3. Add to `.env`: `INTEGRATION_SERVICENOW_PASSWORD=<that password>`, then
   `docker compose restart api celery-worker`.
4. Integrations → **Add connection** → ServiceNow:
   - Instance URL: `https://devNNNNN.service-now.com`
   - Username: `nexus.integration`, Password variable: `INTEGRATION_SERVICENOW_PASSWORD`
   - Leave "Link URL for people" empty
   - **Test settings** → **Add connection** → **Sync now**

No code changes: it's the same connector the mock uses.

Notes:
- Developer instances hibernate when unused. Wake yours from the developer
  portal if Test says it can't connect.
- Push mode needs ServiceNow to reach your NEXUS, which a laptop isn't. Use
  polling, or expose NEXUS with a tunnel (e.g. ngrok) and call the webhook
  from a Business Rule on the incident table.
- `link-existing` against a real instance creates real incidents, so it asks
  for `--confirm-real`.
- Status write-back is off by default. Turn on "Also set the ticket to
  Resolved" only if the client wants NEXUS to close tickets; it sets
  `close_code` and `close_notes` as ServiceNow requires.

---

## 4. Add another source system

### Option A: no code (Generic REST connector)

If the system has a JSON API that lists tickets, add a **Generic REST API**
connection and describe where each field is. Example for a Jira-like API:

```json
{
  "base_url": "https://acme.atlassian.net",
  "list_path": "/rest/api/3/search",
  "auth_type": "basic", "username": "bot@acme.com", "password_env": "INTEGRATION_JIRA_TOKEN",
  "extra_params": {"jql": "project = ITSM ORDER BY updated ASC"},
  "items_path": "issues",
  "field_map": {
    "external_id": "id", "external_number": "key",
    "title": "fields.summary", "description": "fields.description",
    "priority": "fields.priority.name", "status": "fields.status.name",
    "updated_at": "fields.updated"
  },
  "mappings": {
    "priority": {"highest": "critical", "high": "high", "medium": "medium", "low": "low"},
    "status": {"to do": "open", "in progress": "in_progress", "done": "resolved"}
  },
  "ticket_url_template": "https://acme.atlassian.net/browse/{external_number}",
  "note_method": "POST",
  "note_path_template": "/rest/api/2/issue/{external_id}/comment",
  "note_body_template": {"body": "{text}"}
}
```

The test suite proves this works by reading the mock ServiceNow through the
generic connector with nothing but field paths.

### Option B: a dedicated connector (about 60 lines)

Create `backend/app/integrations/connectors/jira.py`:

```python
from app.integrations.base import TicketConnector
from app.integrations.registry import register_connector
from app.integrations.schemas import ConfigField, ConnectionTestResult, ExternalTicket

@register_connector
class JiraConnector(TicketConnector):
    type = "jira"
    display_name = "Jira Service Management"
    config_fields = [
        ConfigField(key="site_url", label="Site URL", type="url", required=True),
        ConfigField(key="email", label="Bot account email", required=True),
        ConfigField(key="token_env", label="API token variable", type="env_var", required=True),
    ]
    default_mappings = {"priority": {"highest": "critical", "high": "high", "medium": "medium", "low": "low"}}

    async def test_connection(self) -> ConnectionTestResult: ...
    async def fetch_updated(self, since, limit=500) -> list[ExternalTicket]: ...
    async def post_note(self, ticket, text) -> None: ...
```

Then add `jira` to the import in `connectors/__init__.py`. The Integrations
page builds its form from `config_fields`, and the sync engine, write-back,
API and UI work unchanged.

---

## 5. How syncing behaves

| Rule | Why |
|---|---|
| One NEXUS ticket per external ticket (unique per connection) | Re-syncing never duplicates |
| Unchanged tickets are skipped by content hash | NEXUS's own work notes bump ServiceNow's "updated" time; they don't cause re-imports or re-runs |
| Title, description, priority and category follow the source | The source system is the system of record |
| Status follows the source only when the source's status changes | A ticket NEXUS resolved isn't reopened by an unrelated edit |
| Agents run only on tickets that arrive **open**, once | Historical and closed tickets don't cost LLM calls |
| Each ticket is saved in its own transaction | One bad record can't fail the batch; it's retried next sync |
| One sync per connection at a time (Postgres advisory lock) | The schedule, "Sync now" and push can't collide |
| The cursor never passes a ticket that failed | Nothing is silently lost |
| The same note is never posted twice | Write-back is safe to retry |
| Imported tickets are created by an inactive `integrations` user | It has no password and can't log in |
| Credentials only in `INTEGRATION_*` env vars | The database and API never hold or return secrets |

---

## 6. Reference

### API (`/api/v1/integrations`)

| Method | Path | Who |
|---|---|---|
| GET | `/types` | analyst, agent, admin |
| GET | `/` , `/{id}` , `/{id}/runs` | analyst, agent, admin |
| POST | `/` , `/test` | admin |
| PATCH / DELETE | `/{id}` (delete only if no tickets came from it) | admin |
| POST | `/{id}/test` | admin |
| POST | `/{id}/sync?full=false&wait=false` | agent, admin |
| POST | `/{id}/webhook` (header `X-Nexus-Webhook-Token`) | the source system |
| POST | `/tickets/{ticket_id}/write-back` | agent, admin |

Ticket responses now include `source` (`nexus` or e.g. `servicenow`),
`source_name`, `external_number` and `external_url`.

### Command line

```powershell
docker compose exec api python -m app.integrations.cli list
docker compose exec api python -m app.integrations.cli sync --connection "ServiceNow (mock)" [--full]
docker compose exec api python -m app.integrations.cli setup-mock
docker compose exec api python -m app.integrations.cli link-existing [--dry-run] [--confirm-real]
```

### Tests

```powershell
cd backend
pytest tests/test_integrations.py -v
```

28 tests: mappings, secret-name rules, both connectors against the in-process
mock, the sync engine (import, idempotency, status rules, failure isolation,
write-back), `link-existing`, the API with role checks, and the webhook. They
use the same `nexusai_test` database as the other tests, never the one in `.env`.

### Files

New:
- `backend/app/integrations/` (framework, connectors, sync engine, CLI)
- `backend/app/models/integration.py`, `backend/app/schemas/integration.py`
- `backend/app/api/v1/integrations.py`, `backend/app/workers/integration_tasks.py`
- `backend/alembic/versions/b7c8d9e0f1a2_add_integration_tables.py`
- `backend/tests/test_integrations.py`
- `mock_servicenow/` (app, Dockerfile)
- `frontend/src/pages/IntegrationsPage.jsx`, `frontend/src/components/SourceBadge.jsx`

Changed (small, additive):
- `models/ticket.py` (`external_link` relationship + source properties), `schemas/ticket.py` (4 fields)
- `workers/celery_app.py` (new task module, a beat entry, write-back after agent runs)
- `main.py` (router), `config.py` (`PUBLIC_APP_URL`), `database.py`, `models/__init__.py`
- `alembic/env.py` (syntax fix + new tables)
- `App.jsx`, `Layout.jsx`, `api/tickets.js`, `TicketsPage.jsx`, `TicketDetailPage.jsx`
- `docker-compose.yml` (`servicenow-mock`), `.env.example`
