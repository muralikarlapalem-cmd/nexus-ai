"""add ticket_status_history table

Revision ID: a1b2c3d4e5f6
Revises: 0001_initial_schema
Create Date: 2026-09-13 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '0001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ticket_status_history',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('ticket_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('old_status', postgresql.ENUM('open', 'triaged', 'in_progress', 'awaiting_user',
                                                   'resolved', 'closed', 'cancelled',
                                                   name='ticket_status', create_type=False), nullable=True),
        sa.Column('new_status', postgresql.ENUM('open', 'triaged', 'in_progress', 'awaiting_user',
                                                   'resolved', 'closed', 'cancelled',
                                                   name='ticket_status', create_type=False), nullable=False),
        sa.Column('changed_by_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id']),
        sa.ForeignKeyConstraint(['changed_by_id'], ['users.id']),
    )
    op.create_index('ix_ticket_status_history_ticket_id', 'ticket_status_history', ['ticket_id'])
    op.create_index('ix_ticket_status_history_changed_at', 'ticket_status_history', ['changed_at'])


def downgrade() -> None:
    op.drop_index('ix_ticket_status_history_changed_at', table_name='ticket_status_history')
    op.drop_index('ix_ticket_status_history_ticket_id', table_name='ticket_status_history')
    op.drop_table('ticket_status_history')