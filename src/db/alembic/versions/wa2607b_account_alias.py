"""account_alias table

Revision ID: wa2607b_account_alias
Revises: wa2607a_adjclose_splits
Create Date: 2026-07-07 00:00:05.000000

REQ-FIX-WLT-004 (wealth design §4.2). Additive ``account_alias`` table mirroring
the sparkry-crm D1 schema: legacy XLSX ``raw_account_name`` (stored lowercased,
PK) → live ``account.id``. Drives the per-name effective-cutoff dedup in
networth-history so a late-onboarding live account no longer wrongly zeroes its
legacy twin at the first account's cutover.

Downgrade drops the table (a real reversal; no protected-table rows).

Chains onto ``wa2607a_adjclose_splits`` (in-branch predecessor) per the ledger.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "wa2607b_account_alias"
down_revision: str | None = "wa2607a_adjclose_splits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_alias",
        sa.Column("raw_account_name", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name="fk_account_alias_account_id"
        ),
        sa.PrimaryKeyConstraint("raw_account_name", name="pk_account_alias"),
    )
    op.create_index(
        "ix_account_alias_account_id", "account_alias", ["account_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_account_alias_account_id", table_name="account_alias")
    op.drop_table("account_alias")
