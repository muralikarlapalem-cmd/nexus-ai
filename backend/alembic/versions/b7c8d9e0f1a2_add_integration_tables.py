"""add integration tables (external ticketing systems)

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-11-18 20:00:00.000000

Adds integration_connections, external_ticket_links and integration_sync_runs.
The tickets table is not changed.

Safe to run on a database where the API already created these tables at
startup (development mode calls Base.metadata.create_all): existing tables
are left alone.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exists(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _exists("integration_connections"):
        op.create_table(
            "integration_connections",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False, unique=True),
            sa.Column("connector_type", sa.String(50), nullable=False),
            sa.Column("config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sync_interval_minutes", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("auto_run_agents", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("write_back_notes", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("write_back_status", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("webhook_secret_env", sa.String(128), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_sync_status", sa.String(20), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_by_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_integration_connections_connector_type", "integration_connections", ["connector_type"])

    if not _exists("external_ticket_links"):
        op.create_table(
            "external_ticket_links",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("ticket_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("connection_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("integration_connections.id"), nullable=False),
            sa.Column("source_type", sa.String(50), nullable=False),
            sa.Column("source_name", sa.String(100), nullable=False),
            sa.Column("external_id", sa.String(255), nullable=False),
            sa.Column("external_number", sa.String(100), nullable=True),
            sa.Column("external_url", sa.Text(), nullable=True),
            sa.Column("external_status", sa.String(30), nullable=True),
            sa.Column("external_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("last_note_hash", sa.String(64), nullable=True),
            sa.Column("last_written_back_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_write_back_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("connection_id", "external_id", name="uq_external_ticket_per_connection"),
        )
        op.create_index("ix_external_ticket_links_connection_id", "external_ticket_links", ["connection_id"])
        op.create_index("ix_external_ticket_links_external_number", "external_ticket_links", ["external_number"])

    if not _exists("integration_sync_runs"):
        op.create_table(
            "integration_sync_runs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("connection_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("integration_connections.id", ondelete="CASCADE"), nullable=False),
            sa.Column("trigger", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unchanged", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("agents_queued", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("details", postgresql.JSONB(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_integration_sync_runs_connection_id", "integration_sync_runs", ["connection_id"])
        op.create_index("ix_integration_sync_runs_started_at", "integration_sync_runs", ["started_at"])


def downgrade() -> None:
    op.drop_table("integration_sync_runs")
    op.drop_table("external_ticket_links")
    op.drop_table("integration_connections")
