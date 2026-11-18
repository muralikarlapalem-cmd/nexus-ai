"""
NEXUS AI · tests/test_integrations.py
Tests for the connector framework, the sync engine and the Integrations API.

The mock ServiceNow app runs in-process (no Docker, no network): connectors
talk to it through an httpx ASGI transport. Database tests use the same
nexusai_test database as the other test modules, and point the sync engine's
sessions at it explicitly, so they never touch the database in .env.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.database as app_database
import app.integrations.registry as registry
import app.integrations.sync as sync_module
from app.core.security import create_access_token, hash_password
from app.database import Base, get_async_db
from app.integrations.connectors.generic_rest import GenericRestConnector, get_path
from app.integrations.connectors.servicenow import ServiceNowConnector
from app.integrations.mapping import ValueMapper
from app.integrations.registry import build_connector, list_connector_types
from app.integrations.secrets import SecretError, validate_env_var_name
from app.main import app
from app.models.integration import ExternalTicketLink, IntegrationConnection
from app.models.ticket import AssignedAgent, Ticket, TicketPriority, TicketStatus, TicketStatusHistory
from app.models.user import User, UserRole

TEST_DB_URL = "postgresql+asyncpg://nexus:nexus_secret@localhost:15432/nexusai_test"
MOCK_PATH = Path(__file__).resolve().parents[2] / "mock_servicenow" / "app.py"
SN_PASSWORD = "mock-test-password"
WEBHOOK_SECRET = "webhook-test-secret"


# ─────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────
@pytest.fixture
def mock_sn(tmp_path, monkeypatch):
    """A fresh mock ServiceNow with 3 demo incidents, wired into every connector."""
    spec = importlib.util.spec_from_file_location("mock_servicenow_app", MOCK_PATH)
    mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    mock.DB_PATH = str(tmp_path / "servicenow.db")
    mock.API_PASSWORD = SN_PASSWORD
    mock.WEBHOOK_URL = ""
    mock.init_db()
    mock.seed_demo()
    monkeypatch.setenv("INTEGRATION_SN_TEST_PASSWORD", SN_PASSWORD)
    monkeypatch.setenv("INTEGRATION_SN_TEST_WEBHOOK", WEBHOOK_SECRET)
    monkeypatch.setattr(registry, "HTTP_TRANSPORT_OVERRIDE", ASGITransport(app=mock.app))
    return mock


SN_CONFIG = {
    "instance_url": "http://servicenow-mock:8090",
    "browser_url": "http://localhost:8090",
    "username": "nexus.integration",
    "password_env": "INTEGRATION_SN_TEST_PASSWORD",
    "initial_sync_days": 30,
}


@pytest.fixture
def dispatched(monkeypatch):
    """Record agent dispatches instead of sending them to Celery."""
    calls: list[str] = []
    monkeypatch.setattr(sync_module, "_default_dispatch", calls.append)
    return calls


@pytest_asyncio.fixture
async def db_session_factory(monkeypatch):
    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    # The sync engine opens its own sessions: point them at the test database
    monkeypatch.setattr(app_database, "AsyncSessionLocal", factory)
    monkeypatch.setattr(sync_module, "AsyncSessionLocal", factory)

    async def override_get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_async_db] = override_get_db
    yield factory
    app.dependency_overrides.pop(get_async_db, None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _make_user(factory, username: str, role: UserRole) -> tuple[User, str]:
    async with factory() as db:
        user = User(email=f"{username}@nexus.ai", username=username, full_name=username,
                    role=role, hashed_password=hash_password("testpass123"))
        db.add(user)
        await db.commit()
        await db.refresh(user)
    token = create_access_token(subject=str(user.id), extra_claims={"role": role.value, "email": user.email})
    return user, token


async def _make_connection(factory, **overrides) -> IntegrationConnection:
    async with factory() as db:
        connection = IntegrationConnection(
            name=overrides.pop("name", "ServiceNow (test)"), connector_type="servicenow",
            config=ServiceNowConnector.validate_config({**SN_CONFIG, **overrides.pop("config", {})}),
            **overrides,
        )
        db.add(connection)
        await db.commit()
        await db.refresh(connection)
        return connection


async def _tickets(factory) -> list[Ticket]:
    async with factory() as db:
        return (await db.execute(select(Ticket).order_by(Ticket.ticket_number))).scalars().all()


# ─────────────────────────────────────────────────────────────
#  Mapping, secrets, registry (no database)
# ─────────────────────────────────────────────────────────────
def test_value_mapper_defaults_and_overrides():
    mapper = ValueMapper(ServiceNowConnector.default_mappings, {"category": {"db": "database"}})
    assert mapper.to_nexus("priority", "1") == "critical"
    assert mapper.to_nexus("priority", " 5 ") == "low"
    assert mapper.to_nexus("priority", "99") == "medium"          # unknown → default
    assert mapper.to_nexus("category", "DB") == "database"        # override, case-insensitive
    assert mapper.to_nexus("status", "") == "open"
    assert mapper.to_source("status", "resolved") == "6"


def test_value_mapper_rejects_unknown_nexus_values():
    with pytest.raises(ValueError, match="priority"):
        ValueMapper({}, {"priority": {"1": "urgent"}})


def test_env_var_names_must_use_integration_prefix():
    assert validate_env_var_name("INTEGRATION_JIRA_TOKEN") == "INTEGRATION_JIRA_TOKEN"
    for bad in ("OPENAI_API_KEY", "JWT_SECRET_KEY", "integration_lower", "INTEGRATION-DASH", ""):
        with pytest.raises(SecretError):
            validate_env_var_name(bad)


def test_config_validation_blocks_non_integration_secrets():
    with pytest.raises(ValueError, match="INTEGRATION_"):
        ServiceNowConnector.validate_config({**SN_CONFIG, "password_env": "OPENAI_API_KEY"})
    with pytest.raises(ValueError, match="Instance URL"):
        ServiceNowConnector.validate_config({"username": "x"})


def test_registry_lists_built_in_connectors():
    types = {t["type"] for t in list_connector_types()}
    assert {"servicenow", "generic_rest"} <= types


def test_get_path_reads_nested_values():
    data = {"fields": {"priority": {"name": "High"}, "labels": ["a", "b"]}}
    assert get_path(data, "fields.priority.name") == "High"
    assert get_path(data, "fields.labels.1") == "b"
    assert get_path(data, "fields.missing.x") is None


# ─────────────────────────────────────────────────────────────
#  Connectors against the mock (no database)
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_servicenow_connector_reads_and_maps_incidents(mock_sn):
    connector = build_connector("servicenow", ServiceNowConnector.validate_config(SN_CONFIG))
    assert (await connector.test_connection()).ok
    tickets = await connector.fetch_updated(None)
    assert len(tickets) == 3
    vpn = next(t for t in tickets if "VPN" in t.title)
    assert vpn.priority == "high" and vpn.category == "network" and vpn.status == "open"
    assert vpn.reporter == "Priya Raman" and vpn.assignment_group == "Network Team"
    assert vpn.external_number.startswith("INC")
    assert vpn.external_url.startswith("http://localhost:8090/nav_to.do")


@pytest.mark.asyncio
async def test_servicenow_connector_posts_work_notes(mock_sn):
    connector = build_connector("servicenow", ServiceNowConnector.validate_config(SN_CONFIG))
    ticket = (await connector.fetch_updated(None))[0]
    await connector.post_note(ticket, "Suggested fix from NEXUS")
    notes = mock_sn.journal_for(ticket.external_id)
    assert notes[0]["element"] == "work_notes" and notes[0]["value"] == "Suggested fix from NEXUS"


@pytest.mark.asyncio
async def test_servicenow_connector_reports_bad_credentials(mock_sn, monkeypatch):
    monkeypatch.setenv("INTEGRATION_SN_TEST_PASSWORD", "wrong")
    result = await build_connector("servicenow", ServiceNowConnector.validate_config(SN_CONFIG)).test_connection()
    assert not result.ok and "401" in result.message


@pytest.mark.asyncio
async def test_servicenow_since_filter(mock_sn):
    connector = build_connector("servicenow", ServiceNowConnector.validate_config(SN_CONFIG))
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert await connector.fetch_updated(future) == []


@pytest.mark.asyncio
async def test_generic_rest_connector_needs_no_code(mock_sn):
    """The same mock, read through the generic connector using only field paths."""
    config = GenericRestConnector.validate_config({
        "base_url": "http://servicenow-mock:8090",
        "list_path": "/api/now/table/incident",
        "auth_type": "basic", "username": "nexus.integration", "password_env": "INTEGRATION_SN_TEST_PASSWORD",
        "items_path": "result",
        "extra_params": {"sysparm_display_value": "false"},
        "field_map": {"external_id": "sys_id", "external_number": "number", "title": "short_description",
                      "description": "description", "priority": "priority", "category": "category",
                      "status": "state", "updated_at": "sys_updated_on"},
        "mappings": {"priority": {"2": "high"}, "status": {"1": "open"}},
        "note_method": "PATCH",
        "note_path_template": "/api/now/table/incident/{external_id}",
        "note_body_template": {"work_notes": "{text}"},
    })
    connector = build_connector("generic_rest", config)
    assert (await connector.test_connection()).ok
    tickets = await connector.fetch_updated(None)
    assert len(tickets) == 3
    assert next(t for t in tickets if "VPN" in t.title).priority == "high"
    await connector.post_note(tickets[0], 'Note with "quotes"')
    assert mock_sn.journal_for(tickets[0].external_id)[0]["value"] == 'Note with "quotes"'


# ─────────────────────────────────────────────────────────────
#  Sync engine (database)
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_first_sync_imports_and_queues_agents(mock_sn, db_session_factory, dispatched):
    connection = await _make_connection(db_session_factory)
    summary = await sync_module.sync_connection(connection.id)
    assert summary["status"] == "success"
    assert (summary["fetched"], summary["created"], summary["agents_queued"]) == (3, 3, 3)

    tickets = await _tickets(db_session_factory)
    assert len(tickets) == 3 and len(dispatched) == 3
    assert all(t.ticket_number.startswith("NX-") for t in tickets)
    async with db_session_factory() as db:
        links = (await db.execute(select(ExternalTicketLink))).scalars().all()
        bot = (await db.execute(select(User).where(User.username == "integrations"))).scalar_one()
        history = (await db.execute(select(TicketStatusHistory))).scalars().all()
    assert {l.source_type for l in links} == {"servicenow"}
    assert all(t.created_by_id == bot.id for t in tickets) and bot.is_active is False
    assert len(history) == 3 and all("Imported from" in h.reason for h in history)


@pytest.mark.asyncio
async def test_resync_is_idempotent(mock_sn, db_session_factory, dispatched):
    connection = await _make_connection(db_session_factory)
    await sync_module.sync_connection(connection.id)
    summary = await sync_module.sync_connection(connection.id, full=True)
    assert (summary["created"], summary["updated"], summary["unchanged"]) == (0, 0, 3)
    assert len(await _tickets(db_session_factory)) == 3
    assert len(dispatched) == 3          # agents not re-run


@pytest.mark.asyncio
async def test_source_changes_flow_into_nexus(mock_sn, db_session_factory, dispatched):
    connection = await _make_connection(db_session_factory)
    await sync_module.sync_connection(connection.id)
    incident = next(r for r in mock_sn.all_records("incident") if "VPN" in r["short_description"])
    mock_sn.update("incident", incident["sys_id"],
                   {"state": "6", "short_description": "VPN fixed: certificate renewed"}, "web.user")

    summary = await sync_module.sync_connection(connection.id)
    assert summary["updated"] == 1
    async with db_session_factory() as db:
        link = (await db.execute(select(ExternalTicketLink).where(
            ExternalTicketLink.external_id == incident["sys_id"]))).scalar_one()
        ticket = await db.get(Ticket, link.ticket_id)
    assert ticket.status == TicketStatus.RESOLVED and ticket.resolved_at is not None
    assert ticket.title == "VPN fixed: certificate renewed"


@pytest.mark.asyncio
async def test_unrelated_source_edit_does_not_reopen_a_nexus_resolution(mock_sn, db_session_factory, dispatched):
    connection = await _make_connection(db_session_factory)
    await sync_module.sync_connection(connection.id)
    async with db_session_factory() as db:
        ticket = (await db.execute(select(Ticket).where(Ticket.title.like("%VPN%")))).scalar_one()
        ticket.status = TicketStatus.RESOLVED          # NEXUS agents resolved it
        await db.commit()
        link = (await db.execute(select(ExternalTicketLink).where(
            ExternalTicketLink.ticket_id == ticket.id))).scalar_one()
    mock_sn.update("incident", link.external_id, {"description": "More detail from the user"}, "web.user")
    await sync_module.sync_connection(connection.id)
    async with db_session_factory() as db:
        assert (await db.get(Ticket, ticket.id)).status == TicketStatus.RESOLVED


@pytest.mark.asyncio
async def test_new_incident_in_source_is_picked_up(mock_sn, db_session_factory, dispatched):
    connection = await _make_connection(db_session_factory)
    await sync_module.sync_connection(connection.id)
    mock_sn.create("incident", {"short_description": "Printer on floor 3 offline",
                                "description": "Shows 'offline' since this morning.",
                                "category": "hardware", "impact": "3", "urgency": "2"}, "web.user")
    summary = await sync_module.sync_connection(connection.id)
    assert summary["created"] == 1 and len(dispatched) == 4


@pytest.mark.asyncio
async def test_disabled_agents_and_closed_tickets_are_not_dispatched(mock_sn, db_session_factory, dispatched):
    incident = mock_sn.all_records("incident")[0]
    mock_sn.update("incident", incident["sys_id"], {"state": "7"}, "web.user")
    connection = await _make_connection(db_session_factory)
    summary = await sync_module.sync_connection(connection.id)
    assert summary["agents_queued"] == 2          # the closed one is skipped

    other = await _make_connection(db_session_factory, name="No agents", auto_run_agents=False,
                                   config={"query_filter": "category=network"})
    assert (await sync_module.sync_connection(other.id))["agents_queued"] == 0


@pytest.mark.asyncio
async def test_one_bad_record_does_not_fail_the_batch(mock_sn, db_session_factory, dispatched, monkeypatch):
    connection = await _make_connection(db_session_factory)
    original = sync_module.upsert_external_ticket

    async def flaky(db, conn, ext, bot):
        if "keyboard" in ext.title.lower():
            raise RuntimeError("simulated failure")
        return await original(db, conn, ext, bot)

    monkeypatch.setattr(sync_module, "upsert_external_ticket", flaky)
    summary = await sync_module.sync_connection(connection.id)
    assert summary["status"] == "partial" and summary["created"] == 2 and summary["failed"] == 1
    assert summary["details"]["errors"][0]["error"] == "simulated failure"

    monkeypatch.setattr(sync_module, "upsert_external_ticket", original)
    summary = await sync_module.sync_connection(connection.id)       # retried next time
    assert summary["created"] == 1


@pytest.mark.asyncio
async def test_connection_errors_are_recorded(mock_sn, db_session_factory, dispatched, monkeypatch):
    connection = await _make_connection(db_session_factory)
    monkeypatch.setenv("INTEGRATION_SN_TEST_PASSWORD", "wrong")
    summary = await sync_module.sync_connection(connection.id)
    assert summary["status"] == "failed" and "401" in summary["error"]
    async with db_session_factory() as db:
        conn = await db.get(IntegrationConnection, connection.id)
    assert conn.last_sync_status == "failed" and conn.last_synced_at is None


@pytest.mark.asyncio
async def test_write_back_posts_once_and_does_not_trigger_reimport(mock_sn, db_session_factory, dispatched):
    connection = await _make_connection(db_session_factory)
    await sync_module.sync_connection(connection.id)
    async with db_session_factory() as db:
        ticket = (await db.execute(select(Ticket).where(Ticket.title.like("%ORA-01555%")))).scalar_one()
        ticket.triage_output = {"priority": "high", "category": "database", "confidence": 0.86}
        ticket.diagnosis_output = {
            "root_cause": "Undo retention too short for the batch's long-running query",
            "resolution_steps": [{"step": 1, "action": "Increase undo retention",
                                  "command": "ALTER SYSTEM SET undo_retention = 7200;"}],
            "verification_steps": ["Re-run the batch"],
        }
        ticket.resolution_notes = "Root Cause: undo retention"
        ticket.status = TicketStatus.RESOLVED
        await db.commit()

    first = await sync_module.write_back_ticket(ticket.id)
    second = await sync_module.write_back_ticket(ticket.id)
    assert first["status"] == "posted" and second["status"] == "already_posted"

    async with db_session_factory() as db:
        link = (await db.execute(select(ExternalTicketLink).where(
            ExternalTicketLink.ticket_id == ticket.id))).scalar_one()
    notes = mock_sn.journal_for(link.external_id)
    assert len(notes) == 1
    assert "ALTER SYSTEM SET undo_retention" in notes[0]["value"]
    assert "suggested resolution ready" in notes[0]["value"]
    assert mock_sn.get_record("incident", link.external_id)["state"] == "1"   # status left alone

    summary = await sync_module.sync_connection(connection.id, full=True)
    assert summary["created"] == 0 and summary["updated"] == 0


@pytest.mark.asyncio
async def test_write_back_status_when_enabled(mock_sn, db_session_factory, dispatched):
    connection = await _make_connection(db_session_factory, write_back_status=True)
    await sync_module.sync_connection(connection.id)
    async with db_session_factory() as db:
        ticket = (await db.execute(select(Ticket).where(Ticket.title.like("%keyboard%")))).scalar_one()
        ticket.resolution_notes = "Rolled back the driver"
        ticket.status = TicketStatus.RESOLVED
        await db.commit()
    result = await sync_module.write_back_ticket(ticket.id)
    assert result["actions"] == ["note", "status"]
    async with db_session_factory() as db:
        link = (await db.execute(select(ExternalTicketLink).where(
            ExternalTicketLink.ticket_id == ticket.id))).scalar_one()
    record = mock_sn.get_record("incident", link.external_id)
    assert record["state"] == "6" and record["close_code"]


@pytest.mark.asyncio
async def test_write_back_skips_tickets_without_agent_output_or_link(mock_sn, db_session_factory, dispatched):
    connection = await _make_connection(db_session_factory)
    await sync_module.sync_connection(connection.id)
    ticket = (await _tickets(db_session_factory))[0]
    assert (await sync_module.write_back_ticket(ticket.id))["status"] == "nothing_to_post"

    user, _ = await _make_user(db_session_factory, "local", UserRole.USER)
    async with db_session_factory() as db:
        local = Ticket(ticket_number="NX-2026-99999", title="Local ticket", description="Made in NEXUS",
                       created_by_id=user.id)
        db.add(local)
        await db.commit()
    assert (await sync_module.write_back_ticket(local.id))["status"] == "not_linked"


@pytest.mark.asyncio
async def test_needs_human_review_note(mock_sn, db_session_factory, dispatched):
    connection = await _make_connection(db_session_factory)
    await sync_module.sync_connection(connection.id)
    async with db_session_factory() as db:
        ticket = (await db.execute(select(Ticket).limit(1))).scalar_one()
        ticket.assigned_agent = AssignedAgent.HUMAN
        ticket.status = TicketStatus.IN_PROGRESS
        await db.commit()
    assert (await sync_module.write_back_ticket(ticket.id))["status"] == "posted"


@pytest.mark.asyncio
async def test_link_existing_tickets(mock_sn, db_session_factory, dispatched):
    """Existing NEXUS tickets copied into the mock and linked, then a sync finds nothing new."""
    from app.integrations import cli

    user, _ = await _make_user(db_session_factory, "creator", UserRole.USER)
    async with db_session_factory() as db:
        db.add_all([
            Ticket(ticket_number="NX-2026-00001", title="Oracle error 01555", description="Batch failed",
                   priority=TicketPriority.HIGH, created_by_id=user.id),
            Ticket(ticket_number="NX-2026-00002", title="Login not working", description="SSO loop",
                   status=TicketStatus.RESOLVED, resolution_notes="Cleared cookies", created_by_id=user.id),
        ])
        await db.commit()
    connection = await _make_connection(db_session_factory, name="ServiceNow (mock)")
    assert await cli.link_existing("ServiceNow (mock)", 100, dry_run=False, confirm_real=False) == 0

    records = {r["number"]: r for r in mock_sn.all_records("incident")}
    assert records["INC0000001"]["priority"] == "2" and records["INC0000002"]["state"] == "6"
    tickets = {t.ticket_number: t for t in await _tickets(db_session_factory)}
    assert records["INC0000001"]["sys_id"] == tickets["NX-2026-00001"].id.hex

    summary = await sync_module.sync_connection(connection.id, full=True)
    assert summary["created"] == 3     # only the 3 demo incidents; the linked tickets are unchanged
    assert summary["unchanged"] == 2


# ─────────────────────────────────────────────────────────────
#  API
# ─────────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_api_full_flow(mock_sn, db_session_factory, dispatched, client):
    _, admin = await _make_user(db_session_factory, "admin1", UserRole.ADMIN)
    _, agent = await _make_user(db_session_factory, "agent1", UserRole.AGENT)
    _, end_user = await _make_user(db_session_factory, "user1", UserRole.USER)

    types = await client.get("/api/v1/integrations/types", headers=_auth(agent))
    assert types.status_code == 200 and any(t["type"] == "servicenow" for t in types.json())

    body = {"name": "ServiceNow (API)", "connector_type": "servicenow", "config": SN_CONFIG,
            "webhook_secret_env": "INTEGRATION_SN_TEST_WEBHOOK"}
    assert (await client.post("/api/v1/integrations/", json=body, headers=_auth(agent))).status_code == 403
    assert (await client.get("/api/v1/integrations/", headers=_auth(end_user))).status_code == 403

    bad = {**body, "config": {**SN_CONFIG, "password_env": "JWT_SECRET_KEY"}}
    assert (await client.post("/api/v1/integrations/", json=bad, headers=_auth(admin))).status_code == 422

    created = await client.post("/api/v1/integrations/", json=body, headers=_auth(admin))
    assert created.status_code == 201, created.text
    connection = created.json()
    assert connection["credentials"] == {"INTEGRATION_SN_TEST_PASSWORD": True, "INTEGRATION_SN_TEST_WEBHOOK": True}
    assert SN_PASSWORD not in created.text
    assert (await client.post("/api/v1/integrations/", json=body, headers=_auth(admin))).status_code == 409

    test = await client.post(f"/api/v1/integrations/{connection['id']}/test", headers=_auth(admin))
    assert test.json()["ok"] is True

    sync = await client.post(f"/api/v1/integrations/{connection['id']}/sync?wait=true", headers=_auth(agent))
    assert sync.status_code == 200 and sync.json()["run"]["created"] == 3

    runs = await client.get(f"/api/v1/integrations/{connection['id']}/runs", headers=_auth(agent))
    assert runs.json()[0]["status"] == "success"

    listed = await client.get("/api/v1/tickets/", headers=_auth(agent))
    items = listed.json()["items"]
    assert {i["source"] for i in items} == {"servicenow"}
    assert all(i["external_number"].startswith("INC") and i["source_name"] == "ServiceNow (API)" for i in items)
    detail = await client.get(f"/api/v1/tickets/{items[0]['id']}", headers=_auth(agent))
    assert detail.json()["external_url"].startswith("http://localhost:8090")

    delete = await client.delete(f"/api/v1/integrations/{connection['id']}", headers=_auth(admin))
    assert delete.status_code == 409

    patched = await client.patch(f"/api/v1/integrations/{connection['id']}",
                                 json={"enabled": False, "sync_interval_minutes": 10}, headers=_auth(admin))
    assert patched.json()["enabled"] is False and patched.json()["sync_interval_minutes"] == 10


@pytest.mark.asyncio
async def test_api_test_unsaved_settings(mock_sn, db_session_factory, client):
    _, admin = await _make_user(db_session_factory, "admin2", UserRole.ADMIN)
    result = await client.post("/api/v1/integrations/test",
                               json={"connector_type": "servicenow", "config": SN_CONFIG}, headers=_auth(admin))
    assert result.json()["ok"] is True
    unknown = await client.post("/api/v1/integrations/test",
                                json={"connector_type": "nope", "config": {}}, headers=_auth(admin))
    assert unknown.status_code == 422


@pytest.mark.asyncio
async def test_webhook_requires_the_shared_secret(mock_sn, db_session_factory, dispatched, client):
    connection = await _make_connection(db_session_factory, webhook_secret_env="INTEGRATION_SN_TEST_WEBHOOK")
    record = mock_sn.create("incident", {"short_description": "Pushed: disk full on app01",
                                         "category": "hardware"}, "web.user")
    payload = {"result": mock_sn.render(record, "incident", None, "all", True, "")}
    url = f"/api/v1/integrations/{connection.id}/webhook"

    assert (await client.post(url, json=payload)).status_code == 404
    assert (await client.post(url, json=payload, headers={"X-Nexus-Webhook-Token": "wrong"})).status_code == 404
    ok = await client.post(url, json=payload, headers={"X-Nexus-Webhook-Token": WEBHOOK_SECRET})
    assert ok.status_code == 200 and ok.json()["created"] == 1 and len(dispatched) == 1

    again = await client.post(url, json=payload, headers={"X-Nexus-Webhook-Token": WEBHOOK_SECRET})
    assert again.json()["unchanged"] == 1


@pytest.mark.asyncio
async def test_unlinked_connection_can_be_deleted(mock_sn, db_session_factory, client):
    _, admin = await _make_user(db_session_factory, "admin3", UserRole.ADMIN)
    connection = await _make_connection(db_session_factory)
    response = await client.delete(f"/api/v1/integrations/{connection.id}", headers=_auth(admin))
    assert response.status_code == 204
