"""
NEXUS AI · app/api/v1/integrations.py
Connections to external ticketing systems.

  GET    /integrations/types                 available connector types + their settings
  GET    /integrations/                      all connections
  POST   /integrations/                      add a connection                  (admin)
  POST   /integrations/test                  test settings before saving       (admin)
  GET    /integrations/{id}                  one connection
  PATCH  /integrations/{id}                  change a connection               (admin)
  DELETE /integrations/{id}                  remove (only if nothing imported) (admin)
  POST   /integrations/{id}/test             test a saved connection           (admin)
  POST   /integrations/{id}/sync             sync now                          (agent/admin)
  GET    /integrations/{id}/runs             sync history
  POST   /integrations/{id}/webhook          push endpoint for the source system (shared-secret header)
  POST   /integrations/tickets/{ticket_id}/write-back   re-post the agents' result (agent/admin)
"""

import secrets as pysecrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_admin, require_agent_or_admin, require_analyst_or_above
from app.database import get_async_db
from app.integrations import secrets as int_secrets
from app.integrations.registry import build_connector, get_connector_class, list_connector_types
from app.models.audit_log import AuditLog
from app.models.integration import ExternalTicketLink, IntegrationConnection, IntegrationSyncRun
from app.models.user import User
from app.schemas.integration import (
    ConnectionCreate, ConnectionResponse, ConnectionTestRequest, ConnectionUpdate,
    SyncRunResponse, TestResultResponse,
)

router = APIRouter(prefix="/integrations", tags=["Integrations"])


# ── Helpers ───────────────────────────────────────────────────
def _validate(connector_type: str, config: dict) -> dict:
    try:
        cls = get_connector_class(connector_type)
        return cls.validate_config(config)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


def _validate_webhook_env(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return int_secrets.validate_env_var_name(name)
    except int_secrets.SecretError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


def _credential_status(connection: IntegrationConnection) -> dict[str, bool]:
    cls = get_connector_class(connection.connector_type)
    names = cls.credential_env_vars(connection.config)
    if connection.webhook_secret_env:
        names.append(connection.webhook_secret_env)
    return {n: int_secrets.is_set(n) for n in names if n}


async def _to_response(db: AsyncSession, connection: IntegrationConnection) -> ConnectionResponse:
    linked = (await db.execute(
        select(func.count()).select_from(ExternalTicketLink)
        .where(ExternalTicketLink.connection_id == connection.id)
    )).scalar_one()
    last_run = (await db.execute(
        select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id == connection.id)
        .order_by(IntegrationSyncRun.started_at.desc()).limit(1)
    )).scalar_one_or_none()
    cls = get_connector_class(connection.connector_type)
    return ConnectionResponse(
        id=connection.id, name=connection.name, connector_type=connection.connector_type,
        display_name=cls.display_name, config=connection.config,
        enabled=connection.enabled, sync_interval_minutes=connection.sync_interval_minutes,
        auto_run_agents=connection.auto_run_agents, write_back_notes=connection.write_back_notes,
        write_back_status=connection.write_back_status, webhook_secret_env=connection.webhook_secret_env,
        webhook_url=f"/api/v1/integrations/{connection.id}/webhook" if connection.webhook_secret_env else None,
        credentials=_credential_status(connection),
        last_synced_at=connection.last_synced_at, last_sync_status=connection.last_sync_status,
        last_error=connection.last_error, linked_tickets=linked,
        last_run=SyncRunResponse.model_validate(last_run) if last_run else None,
        created_at=connection.created_at, updated_at=connection.updated_at,
    )


async def _get(db: AsyncSession, connection_id: uuid.UUID) -> IntegrationConnection:
    connection = await db.get(IntegrationConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return connection


# ── Types ─────────────────────────────────────────────────────
@router.get("/types")
async def connector_types(current_user: User = Depends(require_analyst_or_above)):
    return list_connector_types()


# ── CRUD ──────────────────────────────────────────────────────
@router.get("/", response_model=list[ConnectionResponse])
async def list_connections(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_analyst_or_above),
):
    rows = (await db.execute(select(IntegrationConnection).order_by(IntegrationConnection.name))).scalars().all()
    return [await _to_response(db, c) for c in rows]


@router.post("/", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: ConnectionCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_admin),
):
    config = _validate(body.connector_type, body.config)
    connection = IntegrationConnection(
        name=body.name, connector_type=body.connector_type, config=config, enabled=body.enabled,
        sync_interval_minutes=body.sync_interval_minutes, auto_run_agents=body.auto_run_agents,
        write_back_notes=body.write_back_notes, write_back_status=body.write_back_status,
        webhook_secret_env=_validate_webhook_env(body.webhook_secret_env),
        created_by_id=current_user.id,
    )
    db.add(connection)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A connection named '{body.name}' already exists")
    db.add(AuditLog(user_id=current_user.id, action="integration.create", resource_type="integration",
                    resource_id=str(connection.id),
                    details={"name": body.name, "type": body.connector_type}))
    await db.refresh(connection)
    logger.info(f"Integration '{body.name}' ({body.connector_type}) created by {current_user.username}")
    return await _to_response(db, connection)


@router.post("/test", response_model=TestResultResponse)
async def test_unsaved(body: ConnectionTestRequest, current_user: User = Depends(require_admin)):
    config = _validate(body.connector_type, body.config)
    result = await build_connector(body.connector_type, config).test_connection()
    return TestResultResponse(**result.model_dump())


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_analyst_or_above),
):
    return await _to_response(db, await _get(db, connection_id))


@router.patch("/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: uuid.UUID,
    body: ConnectionUpdate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_admin),
):
    connection = await _get(db, connection_id)
    changes = body.model_dump(exclude_unset=True)
    if "config" in changes:
        changes["config"] = _validate(connection.connector_type, changes["config"] or {})
    if "webhook_secret_env" in changes:
        changes["webhook_secret_env"] = _validate_webhook_env(changes["webhook_secret_env"])
    for key, value in changes.items():
        setattr(connection, key, value)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A connection named '{body.name}' already exists")
    db.add(AuditLog(user_id=current_user.id, action="integration.update", resource_type="integration",
                    resource_id=str(connection.id), details={"fields": sorted(changes)}))
    await db.refresh(connection)
    return await _to_response(db, connection)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_admin),
):
    connection = await _get(db, connection_id)
    linked = (await db.execute(
        select(func.count()).select_from(ExternalTicketLink)
        .where(ExternalTicketLink.connection_id == connection.id)
    )).scalar_one()
    if linked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{linked} ticket(s) came from this connection, so it can't be deleted. Turn it off instead.",
        )
    db.add(AuditLog(user_id=current_user.id, action="integration.delete", resource_type="integration",
                    resource_id=str(connection.id), details={"name": connection.name}))
    await db.delete(connection)


# ── Actions ───────────────────────────────────────────────────
@router.post("/{connection_id}/test", response_model=TestResultResponse)
async def test_connection(
    connection_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_admin),
):
    connection = await _get(db, connection_id)
    result = await build_connector(connection.connector_type, connection.config).test_connection()
    return TestResultResponse(**result.model_dump())


@router.post("/{connection_id}/sync")
async def sync_now(
    connection_id: uuid.UUID,
    full: bool = Query(False, description="Ignore the last-sync cursor and re-read everything"),
    wait: bool = Query(False, description="Run in this request instead of on the worker"),
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_agent_or_admin),
):
    connection = await _get(db, connection_id)
    await db.commit()   # release this request's transaction before the sync opens its own
    if wait:
        from app.integrations.sync import sync_connection
        return {"status": "completed", "run": await sync_connection(connection.id, trigger="manual", full=full)}
    from app.workers.integration_tasks import sync_connection_task
    try:
        task = sync_connection_task.delay(str(connection.id), "manual", full)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Could not queue the sync: {e}")
    return {"status": "queued", "task_id": task.id}


@router.get("/{connection_id}/runs", response_model=list[SyncRunResponse])
async def list_runs(
    connection_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(require_analyst_or_above),
):
    await _get(db, connection_id)
    rows = (await db.execute(
        select(IntegrationSyncRun).where(IntegrationSyncRun.connection_id == connection_id)
        .order_by(IntegrationSyncRun.started_at.desc()).limit(limit)
    )).scalars().all()
    return rows


@router.post("/{connection_id}/webhook")
async def receive_webhook(
    connection_id: uuid.UUID,
    request: Request,
    x_nexus_webhook_token: str | None = Header(None),
    db: AsyncSession = Depends(get_async_db),
):
    """
    For source systems that push changes (e.g. a ServiceNow Business Rule).
    Authenticated by the X-Nexus-Webhook-Token header, compared with the
    connection's webhook secret variable. Returns 404 for any failure so the
    endpoint doesn't reveal which connections exist.
    """
    connection = await db.get(IntegrationConnection, connection_id)
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if connection is None or not connection.enabled or not connection.webhook_secret_env:
        raise not_found
    try:
        expected = int_secrets.read_secret(connection.webhook_secret_env)
    except int_secrets.SecretError:
        raise not_found
    if not x_nexus_webhook_token or not pysecrets.compare_digest(x_nexus_webhook_token, expected):
        raise not_found
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object")
    await db.commit()
    from app.integrations.sync import process_webhook
    return await process_webhook(connection.id, payload)


@router.post("/tickets/{ticket_id}/write-back")
async def write_back_now(
    ticket_id: uuid.UUID,
    current_user: User = Depends(require_agent_or_admin),
):
    from app.integrations.sync import write_back_ticket
    result = await write_back_ticket(ticket_id, force=True)
    if result["status"] == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    if result["status"] == "failed":
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result.get("error"))
    return result
