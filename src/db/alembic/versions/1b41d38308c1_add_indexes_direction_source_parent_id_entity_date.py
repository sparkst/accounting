"""add indexes: direction, source, parent_id, (entity, date)

Revision ID: 1b41d38308c1
Revises: a2ad1082b755
Create Date: 2026-03-25 01:25:32

Adds four new indexes on the transactions table to support common query
patterns without full-table scans:
  - direction           — aggregation endpoint filters/groups by direction
  - source              — ingestion dedup and per-adapter queries
  - parent_id           — split-child lookups and cascade-reject queries
  - (entity, date)      — composite covering the most frequent filter pair
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '1b41d38308c1'
down_revision: str | Sequence[str] | None = 'a2ad1082b755'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add performance indexes."""
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.create_index('ix_transactions_direction', ['direction'])
        batch_op.create_index('ix_transactions_source', ['source'])
        batch_op.create_index('ix_transactions_parent_id', ['parent_id'])
        batch_op.create_index('ix_transactions_entity_date', ['entity', 'date'])


def downgrade() -> None:
    """Drop performance indexes."""
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.drop_index('ix_transactions_entity_date')
        batch_op.drop_index('ix_transactions_parent_id')
        batch_op.drop_index('ix_transactions_source')
        batch_op.drop_index('ix_transactions_direction')
