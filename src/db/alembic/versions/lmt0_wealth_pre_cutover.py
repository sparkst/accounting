"""wealth pre-cutover non-destructive widening (LM-T0)

Revision ID: lmt0_wealth_pre_cutover
Revises: plaid_p1_init_6a9b
Create Date: 2026-05-11 00:00:00.000000

Three non-destructive changes that must land BEFORE the Cloudflare cutover so
that the local accounting DB is compatible with the new actor-string format and
the extended Plaid OAuth flow:

1. Widen ``audit_events.changed_by`` from String(8) to String(64).
   Current values ('human', 'auto') are 5 chars or fewer; no data migration
   needed. Accommodates cron actor strings like 'cron:twelve-data-ingest'
   (21 chars) and 'human:<email>' strings from Cloudflare workers.

2. Add nullable ``audit_events.cf_scheduled_time`` BigInteger column.
   Stores Unix epoch milliseconds from Workers ``controller.scheduledTime``
   for cron-initiated audit rows; NULL for human-initiated rows. Defaults to
   NULL so existing rows are unaffected.

3. Widen ``plaid_item.status`` CHECK constraint from ('active', 'disconnected')
   to ('active', 'disconnected', 'pending_oauth', 'abandoned').
   The OAuth PKCE flow needs 'pending_oauth' while awaiting the redirect, and
   'abandoned' covers Items that never completed OAuth. SQLite cannot modify a
   CHECK constraint in-place — batch_alter_table rebuilds the table.

All three changes are purely additive / widening; downgrade reverses them.
Downgrade of change 3 is refused if any ``plaid_item`` row has status
'pending_oauth' or 'abandoned' — silent data truncation is worse than a
loud failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "lmt0_wealth_pre_cutover"
down_revision: str | Sequence[str] | None = "plaid_p1_init_6a9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---------------------------------------------------------------------------
# Plaid item status sets
# ---------------------------------------------------------------------------
_OLD_PLAID_ITEM_STATUSES = ("active", "disconnected")
_NEW_PLAID_ITEM_STATUSES = ("active", "disconnected", "pending_oauth", "abandoned")
_PLAID_SYNC_STATUSES = ("ok", "error", "pending", "institution_down")
_PLAID_ACCOUNT_TYPES = (
    "depository",
    "credit",
    "investment",
    "brokerage",
    "loan",
    "other",
)


def _values_in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # ── 1 & 2: audit_events — widen changed_by + add cf_scheduled_time ──────
    # batch_alter_table is required for SQLite column type changes.
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.alter_column(
            "changed_by",
            existing_type=sa.String(length=8),
            type_=sa.String(length=64),
            nullable=False,
        )
        batch_op.add_column(
            sa.Column(
                "cf_scheduled_time",
                sa.BigInteger(),
                nullable=True,
                comment=(
                    "Unix epoch ms from Workers controller.scheduledTime for "
                    "cron-initiated rows; NULL for human-initiated rows."
                ),
            )
        )

    # ── 3: plaid_item — widen status CHECK ───────────────────────────────────
    # SQLite cannot ALTER a CHECK constraint in-place; batch_alter_table
    # rebuilds the table with the new constraint and copies existing rows.
    with op.batch_alter_table("plaid_item", schema=None) as batch_op:
        batch_op.drop_constraint("ck_plaid_item_status", type_="check")
        batch_op.create_check_constraint(
            "ck_plaid_item_status",
            f"status IN ({_values_in(_NEW_PLAID_ITEM_STATUSES)})",
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Refuse to downgrade if any plaid_item uses the new statuses.
    new_status_rows = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM plaid_item "
            "WHERE status IN ('pending_oauth', 'abandoned')"
        )
    ).scalar_one()
    if new_status_rows:
        raise RuntimeError(
            f"refusing to downgrade: {new_status_rows} plaid_item rows have status "
            "'pending_oauth' or 'abandoned'. Update or remove those rows first."
        )

    # ── 3: plaid_item — restore narrow CHECK ─────────────────────────────────
    with op.batch_alter_table("plaid_item", schema=None) as batch_op:
        batch_op.drop_constraint("ck_plaid_item_status", type_="check")
        batch_op.create_check_constraint(
            "ck_plaid_item_status",
            f"status IN ({_values_in(_OLD_PLAID_ITEM_STATUSES)})",
        )

    # ── 1 & 2: audit_events — narrow changed_by, drop cf_scheduled_time ─────
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_column("cf_scheduled_time")
        batch_op.alter_column(
            "changed_by",
            existing_type=sa.String(length=64),
            type_=sa.String(length=8),
            nullable=False,
        )
