"""REQ-ALERT-010 add alert_dispatch table (EA alert dedup + audit ledger)

Revision ID: al0_add_alert_dispatch
Revises: 63c79e8be034
Create Date: 2026-06-08 00:00:00.000000

Additive only. Creates the alert_dispatch table used to dedupe and audit EA
alert emails. Touches no protected table; downgrade drops only this table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "al0_add_alert_dispatch"
down_revision: str | Sequence[str] | None = "63c79e8be034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_dispatch",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("alert_key", sa.String(), nullable=False),
        sa.Column("occurrence_date", sa.String(length=10), nullable=False),
        sa.Column("alert_type", sa.String(), nullable=False),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "alert_key", "occurrence_date", name="uq_alert_dispatch_key_date"
        ),
    )
    op.create_index(
        "ix_alert_dispatch_alert_key", "alert_dispatch", ["alert_key"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_alert_dispatch_alert_key", table_name="alert_dispatch")
    op.drop_table("alert_dispatch")
