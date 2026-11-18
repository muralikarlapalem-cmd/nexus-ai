"""
NEXUS AI · app/schemas/integration.py
Request/response schemas for /api/v1/integrations.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _clean_name(v: str) -> str:
    v = v.strip()
    if len(v) < 3:
        raise ValueError("Name must be at least 3 characters")
    return v


class ConnectionCreate(BaseModel):
    name: str = Field(..., max_length=100)
    connector_type: str = Field(..., max_length=50)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    sync_interval_minutes: int = Field(5, ge=1, le=1440)
    auto_run_agents: bool = True
    write_back_notes: bool = True
    write_back_status: bool = False
    webhook_secret_env: str | None = Field(None, max_length=128)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _clean_name(v)


class ConnectionUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    sync_interval_minutes: int | None = Field(None, ge=1, le=1440)
    auto_run_agents: bool | None = None
    write_back_notes: bool | None = None
    write_back_status: bool | None = None
    webhook_secret_env: str | None = Field(None, max_length=128)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return None if v is None else _clean_name(v)


class ConnectionTestRequest(BaseModel):
    """Test settings before saving them."""
    connector_type: str
    config: dict[str, Any] = Field(default_factory=dict)


class SyncRunResponse(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    trigger: str
    status: str
    fetched: int
    created: int
    updated: int
    unchanged: int
    failed: int
    agents_queued: int
    error: str | None
    details: dict | None
    started_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class ConnectionResponse(BaseModel):
    id: uuid.UUID
    name: str
    connector_type: str
    display_name: str
    config: dict[str, Any]
    enabled: bool
    sync_interval_minutes: int
    auto_run_agents: bool
    write_back_notes: bool
    write_back_status: bool
    webhook_secret_env: str | None
    webhook_url: str | None             # path to give the source system, if the webhook is on
    credentials: dict[str, bool]        # env var name → is it set? (values are never returned)
    last_synced_at: datetime | None
    last_sync_status: str | None
    last_error: str | None
    linked_tickets: int
    last_run: SyncRunResponse | None
    created_at: datetime
    updated_at: datetime


class TestResultResponse(BaseModel):
    ok: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
