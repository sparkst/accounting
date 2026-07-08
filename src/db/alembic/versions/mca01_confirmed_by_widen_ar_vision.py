"""widen transactions.confirmed_by + ar_reminder + vision_promotion

Revision ID: mca01_confirmed_by_widen_ar_vision
Revises: vr_isregex01_vendor_rule_is_regex
Create Date: 2026-07-07 00:00:03.000000

Program 2026-07 feature migration (the last in the program's linear chain, per
the cross-workstream migration ledger). Three additive changes:

1. ``transactions.confirmed_by`` widens ``String(8) → String(64)`` so
   auto-confirm can stamp ``auto:rule:<uuid>`` (REQ-MCA-002). SQLite does not
   enforce VARCHAR length, so this is a semantic/honesty change for the model
   and any strict-length port; the batch recreate copies every row verbatim.
   No CHECK change — the column already holds free strings in entity-mode.

2. New ``ar_reminder`` table (REQ-ARC-*) — draft-for-approval AR reminders,
   UNIQUE(invoice_id, rung) for exactly-once per rung.

3. New ``vision_promotion`` table (REQ-VIS-003) — per-institution shadow-cycle
   promotion ledger.

Downgrade is a real reversal: drop the two new tables and narrow the column.
To avoid a SILENT truncation on a strict-length backend, the downgrade first
normalizes any out-of-domain ``auto:rule:<id>`` value back to plain ``auto``
(the column's pre-widen ``auto | human`` domain) — an explicit, data-preserving
reversal of exactly what the widen enabled, not a DELETE and not a drop of any
protected column. No protected table is dropped; raw_data/created_at/updated_at
are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "mca01_confirmed_by_widen_ar_vision"
down_revision: str | None = "vr_isregex01_vendor_rule_is_regex"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Widen confirmed_by (batch mode recreates the table on SQLite).
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "confirmed_by",
            existing_type=sa.String(length=8),
            type_=sa.String(length=64),
            existing_nullable=False,
            comment="auto | human | auto:rule:<id>",
            existing_comment="auto | human",
        )

    # 2. ar_reminder.
    op.create_table(
        "ar_reminder",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "invoice_id",
            sa.String(length=36),
            sa.ForeignKey("invoices.id"),
            nullable=False,
        ),
        sa.Column("rung", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("draft_subject", sa.Text(), nullable=False),
        sa.Column("draft_body", sa.Text(), nullable=False),
        sa.Column("approval_token", sa.String(length=36), nullable=False),
        sa.Column("approved_via", sa.String(length=16), nullable=True),
        sa.Column("resend_message_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("rung IN (14, 30, 45)", name="ck_ar_reminder_rung"),
        sa.CheckConstraint(
            "status IN ('drafted', 'pending_approval', 'approved', 'sent', "
            "'dismissed', 'failed')",
            name="ck_ar_reminder_status",
        ),
        sa.UniqueConstraint(
            "invoice_id", "rung", name="uq_ar_reminder_invoice_rung"
        ),
    )
    op.create_index(
        "ix_ar_reminder_invoice_id", "ar_reminder", ["invoice_id"]
    )

    # 3. vision_promotion.
    op.create_table(
        "vision_promotion",
        sa.Column("institution", sa.String(length=32), primary_key=True),
        sa.Column(
            "consecutive_clean",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_cycle_at", sa.DateTime(), nullable=True),
        sa.Column("last_report_path", sa.String(length=512), nullable=True),
        sa.Column(
            "promoted", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("promoted_at", sa.DateTime(), nullable=True),
        sa.Column("decision_ref", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("vision_promotion")
    op.drop_index("ix_ar_reminder_invoice_id", table_name="ar_reminder")
    op.drop_table("ar_reminder")

    # Normalize out-of-domain values BEFORE narrowing so a strict-length backend
    # cannot truncate them silently — 'auto:rule:<id>' reverts to plain 'auto'.
    op.execute(
        "UPDATE transactions SET confirmed_by = 'auto' "
        "WHERE confirmed_by LIKE 'auto:rule:%'"
    )
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "confirmed_by",
            existing_type=sa.String(length=64),
            type_=sa.String(length=8),
            existing_nullable=False,
            comment="auto | human",
            existing_comment="auto | human | auto:rule:<id>",
        )
