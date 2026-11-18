"""
NEXUS AI · app/integrations/cli.py
Integration commands, run inside the api container:

    docker compose exec api python -m app.integrations.cli setup-mock
    docker compose exec api python -m app.integrations.cli link-existing --connection "ServiceNow (mock)"
    docker compose exec api python -m app.integrations.cli sync --connection "ServiceNow (mock)"
    docker compose exec api python -m app.integrations.cli list

setup-mock      Create a connection to the mock ServiceNow container.
link-existing   Copy NEXUS tickets that have no source into the source system as
                incidents, and link them, so it looks as if they always came from
                there. Against the mock it keeps your ticket dates. Against a real
                instance it creates real incidents, so it asks for --confirm-real.
sync            Run a sync now and print the result.
list            Show connections and how many tickets each one has.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import func, select

MOCK_NAME = "ServiceNow (mock)"


def _sn_time(dt: datetime | None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _connection_by_name(db, name: str):
    from app.models.integration import IntegrationConnection
    connection = (await db.execute(
        select(IntegrationConnection).where(IntegrationConnection.name == name)
    )).scalar_one_or_none()
    if connection is None:
        print(f"No connection named '{name}'. Run 'list' to see them.")
    return connection


async def setup_mock(name: str) -> int:
    from app.database import get_db_context
    from app.integrations.connectors.servicenow import ServiceNowConnector
    from app.models.integration import IntegrationConnection

    config = ServiceNowConnector.validate_config({
        "instance_url": os.environ.get("MOCK_SN_INTERNAL_URL", "http://servicenow-mock:8090"),
        "browser_url": os.environ.get("MOCK_SN_BROWSER_URL", "http://localhost:8090"),
        "username": os.environ.get("MOCK_SN_USER", "nexus.integration"),
        "password_env": "INTEGRATION_SERVICENOW_MOCK_PASSWORD",
        "initial_sync_days": 30,
    })
    async with get_db_context() as db:
        existing = (await db.execute(
            select(IntegrationConnection).where(IntegrationConnection.name == name)
        )).scalar_one_or_none()
        if existing:
            print(f"'{name}' already exists. Nothing changed.")
            return 0
        db.add(IntegrationConnection(
            name=name, connector_type="servicenow", config=config, enabled=True,
            sync_interval_minutes=2, auto_run_agents=True, write_back_notes=True,
            write_back_status=False, webhook_secret_env="INTEGRATION_SERVICENOW_MOCK_WEBHOOK_SECRET",
        ))
    print(f"Created '{name}' → {config['instance_url']} (syncs every 2 minutes).")
    print("Next: test it on the Integrations page, or run: python -m app.integrations.cli sync "
          f"--connection \"{name}\"")
    return 0


def _ticket_to_incident(ticket, mapper) -> dict:
    """NEXUS ticket → ServiceNow incident fields."""
    impact_urgency = {"critical": ("1", "1"), "high": ("1", "2"), "medium": ("2", "2"), "low": ("3", "3")}
    priority_value = {"critical": "1", "high": "2", "medium": "3", "low": "4"}
    fallback_state = {"open": "1", "triaged": "1", "in_progress": "2", "awaiting_user": "3",
                      "resolved": "6", "closed": "7", "cancelled": "8"}
    status = ticket.status.value
    impact, urgency = impact_urgency[ticket.priority.value]
    digits = "".join(ch for ch in ticket.ticket_number.rsplit("-", 1)[-1] if ch.isdigit()) or "0"
    record = {
        "sys_id": ticket.id.hex,
        "number": f"INC{int(digits):07d}",
        "short_description": ticket.title[:160],
        "description": ticket.description,
        "category": mapper.to_source("category", ticket.category.value) or "inquiry",
        "impact": impact, "urgency": urgency,
        "priority": priority_value[ticket.priority.value],
        "state": mapper.to_source("status", status) or fallback_state.get(status, "1"),
        "sys_created_on": _sn_time(ticket.created_at),
        "sys_updated_on": _sn_time(ticket.updated_at or ticket.created_at),
    }
    if status in ("resolved", "closed"):
        record["close_code"] = "Solved (Permanently)"
        record["close_notes"] = (ticket.resolution_notes or "Resolved in NEXUS AI.")[:4000]
    return record


async def link_existing(name: str, limit: int, dry_run: bool, confirm_real: bool) -> int:
    from app.database import get_db_context
    from app.integrations.base import ConnectorError
    from app.integrations.registry import build_connector
    from app.models.integration import ExternalTicketLink
    from app.models.ticket import Ticket

    async with get_db_context() as db:
        connection = await _connection_by_name(db, name)
        if connection is None:
            return 1
        if connection.connector_type != "servicenow":
            print("link-existing currently supports ServiceNow connections only.")
            return 1
        tickets = (await db.execute(
            select(Ticket).outerjoin(ExternalTicketLink, ExternalTicketLink.ticket_id == Ticket.id)
            .where(ExternalTicketLink.id.is_(None)).order_by(Ticket.created_at).limit(limit)
        )).scalars().all()
        connection_id, config = connection.id, dict(connection.config)

    connector = build_connector("servicenow", config)
    is_mock = "servicenow-mock" in config["instance_url"] or "localhost" in config["instance_url"]
    if not is_mock and not confirm_real:
        print(f"{config['instance_url']} looks like a real ServiceNow instance. This would create "
              f"{len(tickets)} real incidents there. Re-run with --confirm-real if that's what you want.")
        return 1
    print(f"{len(tickets)} NEXUS ticket(s) have no source system yet.")
    if dry_run or not tickets:
        for t in tickets[:10]:
            print(f"  would link {t.ticket_number}  {t.title[:60]}")
        return 0

    records = [_ticket_to_incident(t, connector.mapper) for t in tickets]
    try:
        if is_mock:
            response = await connector._request("POST", "/mock/import/incident", json={"records": records})
            print(f"Mock ServiceNow: {response.json()}")
        else:
            for record in records:
                body = {k: v for k, v in record.items() if k not in ("number", "sys_created_on", "sys_updated_on")}
                await connector._request("POST", f"/api/now/table/{connector.table}", json=body)
    except ConnectorError as e:
        print(f"Could not create the incidents: {e}")
        return 1

    newest = None
    async with get_db_context() as db:
        from app.models.integration import IntegrationConnection
        connection = await db.get(IntegrationConnection, connection_id)
        for ticket, record in zip(tickets, records):
            ext = connector.to_external_ticket(record)
            db.add(ExternalTicketLink(
                ticket_id=ticket.id, connection_id=connection.id,
                source_type=connection.connector_type, source_name=connection.name,
                external_id=ext.external_id, external_number=ext.external_number,
                external_url=ext.external_url, external_status=ext.status,
                external_updated_at=ext.updated_at, content_hash=ext.content_hash(),
            ))
            if ext.updated_at and (newest is None or ext.updated_at > newest):
                newest = ext.updated_at
        if connection.last_synced_at is None or (newest and newest > connection.last_synced_at):
            connection.last_synced_at = newest
    print(f"Linked {len(tickets)} ticket(s) to '{name}'. They now show their ServiceNow number in NEXUS.")
    return 0


async def run_sync(name: str, full: bool) -> int:
    from app.database import get_db_context
    from app.integrations.sync import sync_connection

    async with get_db_context() as db:
        connection = await _connection_by_name(db, name)
        if connection is None:
            return 1
        connection_id = connection.id
    summary = await sync_connection(connection_id, trigger="manual", full=full)
    for key in ("status", "fetched", "created", "updated", "unchanged", "failed", "agents_queued", "error"):
        print(f"  {key:14} {summary.get(key)}")
    return 0 if summary["status"] in ("success", "skipped") else 1


async def list_connections() -> int:
    from app.database import get_db_context
    from app.models.integration import ExternalTicketLink, IntegrationConnection

    async with get_db_context() as db:
        rows = (await db.execute(
            select(IntegrationConnection, func.count(ExternalTicketLink.id))
            .outerjoin(ExternalTicketLink, ExternalTicketLink.connection_id == IntegrationConnection.id)
            .group_by(IntegrationConnection.id).order_by(IntegrationConnection.name)
        )).all()
    if not rows:
        print("No connections yet. Try: python -m app.integrations.cli setup-mock")
    for connection, count in rows:
        state = "on " if connection.enabled else "off"
        print(f"[{state}] {connection.name:30} {connection.connector_type:14} {count:5} tickets  "
              f"last sync: {connection.last_sync_status or '-'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.integrations.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup-mock")
    p.add_argument("--name", default=MOCK_NAME)
    p = sub.add_parser("link-existing")
    p.add_argument("--connection", default=MOCK_NAME)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--confirm-real", action="store_true")
    p = sub.add_parser("sync")
    p.add_argument("--connection", default=MOCK_NAME)
    p.add_argument("--full", action="store_true")
    sub.add_parser("list")
    args = parser.parse_args()

    async def run() -> int:
        from app.database import engine
        engine.echo = False          # development mode logs every SQL statement; too noisy here
        try:
            if args.command == "setup-mock":
                return await setup_mock(args.name)
            if args.command == "link-existing":
                return await link_existing(args.connection, args.limit, args.dry_run, args.confirm_real)
            if args.command == "sync":
                return await run_sync(args.connection, args.full)
            return await list_connections()
        finally:
            await engine.dispose()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
