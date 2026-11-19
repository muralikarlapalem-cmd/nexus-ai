"""add tickets.agent_error (why the last agent run stopped or is being retried)

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-11-19 17:00:00.000000

Safe to run when the API already added the column at startup (development
mode does that): IF NOT EXISTS.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS agent_error TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE tickets DROP COLUMN IF EXISTS agent_error")
