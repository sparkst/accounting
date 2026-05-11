"""plaid phase1 — items, balance snapshots, account FK, audit_event entity columns

Revision ID: plaid_p1_init_6a9b
Revises: p4ext1enum0xt
Create Date: 2026-05-10 19:30:00.000000

Adds Plaid Phase 1 schema:
- ``plaid_item`` table — one row per Plaid Item (institution login), with encrypted
  access_token, status, sync metadata, and CSRF state nonce.
- ``plaid_account_balance_snapshot`` table — daily Plaid Balance snapshot, sibling
  to existing ``account_balance_snapshot``. UNIQUE(account_id, snapshot_date) makes
  double-runs idempotent.
- ``account.plaid_item_id`` + ``account.plaid_account_id`` columns — links a Plaid
  account into the existing Account registry.
- Extends ``audit_events`` with nullable ``entity_id`` + ``entity_type`` columns;
  relaxes ``transaction_id`` to nullable; CHECK enforces exactly-one-of
  (transaction_id, entity_id) so we can record Plaid lifecycle events without
  attaching a fake transaction.

SQLite cannot ALTER a column from NOT NULL to NULL in place; ``batch_alter_table``
recopies the table preserving the few existing rows (transaction_id stays set).

Downgrade refuses to run if any ``audit_events`` row has ``entity_id IS NOT NULL``
and ``transaction_id IS NULL`` — silent data loss is worse than a loud failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "plaid_p1_init_6a9b"
down_revision: str | Sequence[str] | None = "p4ext1enum0xt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PLAID_ITEM_STATUSES = ("active", "disconnected")
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
    # 1. plaid_item
    op.create_table(
        "plaid_item",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("institution_id", sa.String(length=64), nullable=False),
        sa.Column("institution_name", sa.String(length=128), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("cursor", sa.String(length=255), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("last_sync_status", sa.String(length=24), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("consent_expiration_at", sa.DateTime(), nullable=True),
        sa.Column("state_nonce", sa.String(length=64), nullable=True),
        sa.Column("state_nonce_expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", name="uq_plaid_item_item_id"),
        sa.CheckConstraint(
            f"status IN ({_values_in(_PLAID_ITEM_STATUSES)})",
            name="ck_plaid_item_status",
        ),
        sa.CheckConstraint(
            f"last_sync_status IS NULL OR last_sync_status IN ({_values_in(_PLAID_SYNC_STATUSES)})",
            name="ck_plaid_item_last_sync_status",
        ),
    )
    with op.batch_alter_table("plaid_item", schema=None) as batch_op:
        batch_op.create_index("ix_plaid_item_status", ["status"], unique=False)
        batch_op.create_index("ix_plaid_item_institution_id", ["institution_id"], unique=False)

    # 2. plaid_account_balance_snapshot
    op.create_table(
        "plaid_account_balance_snapshot",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("plaid_account_type", sa.String(length=24), nullable=False),
        sa.Column("plaid_account_subtype", sa.String(length=64), nullable=True),
        sa.Column("current_balance", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("available_balance", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("iso_currency_code", sa.String(length=3), nullable=True),
        sa.Column("pulled_at", sa.DateTime(), nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["account.id"], name="fk_plaid_bal_snap_account_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "snapshot_date", name="uq_plaid_bal_snap_acct_date"
        ),
        sa.CheckConstraint(
            f"plaid_account_type IN ({_values_in(_PLAID_ACCOUNT_TYPES)})",
            name="ck_plaid_bal_snap_account_type",
        ),
    )
    with op.batch_alter_table("plaid_account_balance_snapshot", schema=None) as batch_op:
        batch_op.create_index(
            "ix_plaid_bal_snap_account_id", ["account_id"], unique=False
        )
        batch_op.create_index(
            "ix_plaid_bal_snap_snapshot_date", ["snapshot_date"], unique=False
        )

    # 3. account FK columns to plaid_item
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("plaid_item_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("plaid_account_id", sa.String(length=128), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_account_plaid_item_id",
            "plaid_item",
            ["plaid_item_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            "uq_account_plaid_link",
            ["plaid_item_id", "plaid_account_id"],
        )

    # 4. audit_events: nullable transaction_id, add entity_id/entity_type, CHECK
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.alter_column(
            "transaction_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column("entity_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("entity_type", sa.String(length=32), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_audit_events_exactly_one_target",
            "(transaction_id IS NOT NULL AND entity_id IS NULL AND entity_type IS NULL) "
            "OR (transaction_id IS NULL AND entity_id IS NOT NULL AND entity_type IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_audit_events_entity", ["entity_type", "entity_id"], unique=False
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Refuse to downgrade if entity-targeted audit rows exist.
    entity_rows = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id IS NOT NULL "
            "OR transaction_id IS NULL"
        )
    ).scalar_one()
    if entity_rows:
        raise RuntimeError(
            f"refusing to downgrade: {entity_rows} audit_events rows reference an"
            " entity (Plaid lifecycle event). Re-attach to a transaction or delete"
            " them first."
        )
    # Refuse to downgrade if any account is still linked to a plaid_item.
    linked_accounts = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM account WHERE plaid_item_id IS NOT NULL "
            "OR plaid_account_id IS NOT NULL"
        )
    ).scalar_one()
    if linked_accounts:
        raise RuntimeError(
            f"refusing to downgrade: {linked_accounts} account rows still reference"
            " a plaid_item. Disconnect them first."
        )

    # Drop in reverse FK order:
    # audit_events restore → account FK columns drop → balance snapshot drop → plaid_item drop
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_events_entity")
        batch_op.drop_constraint("ck_audit_events_exactly_one_target", type_="check")
        batch_op.drop_column("entity_type")
        batch_op.drop_column("entity_id")
        batch_op.alter_column(
            "transaction_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )

    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.drop_constraint("uq_account_plaid_link", type_="unique")
        batch_op.drop_constraint("fk_account_plaid_item_id", type_="foreignkey")
        batch_op.drop_column("plaid_account_id")
        batch_op.drop_column("plaid_item_id")

    with op.batch_alter_table("plaid_account_balance_snapshot", schema=None) as batch_op:
        batch_op.drop_index("ix_plaid_bal_snap_snapshot_date")
        batch_op.drop_index("ix_plaid_bal_snap_account_id")
    op.drop_table("plaid_account_balance_snapshot")

    with op.batch_alter_table("plaid_item", schema=None) as batch_op:
        batch_op.drop_index("ix_plaid_item_institution_id")
        batch_op.drop_index("ix_plaid_item_status")
    op.drop_table("plaid_item")
