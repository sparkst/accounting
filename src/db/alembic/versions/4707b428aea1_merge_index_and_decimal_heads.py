"""merge index and decimal heads

Revision ID: 4707b428aea1
Revises: 1b41d38308c1, 9cc511c9b845
Create Date: 2026-03-26 01:13:48.251083

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '4707b428aea1'
down_revision: str | Sequence[str] | None = ('1b41d38308c1', '9cc511c9b845')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
