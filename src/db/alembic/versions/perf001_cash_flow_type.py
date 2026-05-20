"""REQ-PERF-001 add brokerage_transaction.cash_flow_type column + CHECK

Revision ID: perf001_cash_flow_type
Revises: lmt0_wealth_pre_cutover
Create Date: 2026-05-20 00:00:00.000000

Adds a single new column ``cash_flow_type`` to ``brokerage_transaction`` plus a
CHECK constraint binding it to four enum VALUES (not member names):
``external_in``, ``external_out``, ``internal``, ``none``.

Default is ``'none'`` for existing rows. The backfill is a separate, idempotent
script (REQ-PERF-003) that walks every row through
``src.analytics.classify.classify(tx, PortfolioScope)`` and sets the column.

SQLite cannot ALTER COLUMN to add a NOT NULL column with a CHECK constraint in
place — ``batch_alter_table`` recopies the table, sets the server default, and
recreates constraints in a single transaction.

Downgrade refuses to run if any row has a non-``none`` ``cash_flow_type`` —
silent data loss is worse than a loud failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ── Revision identifiers ──────────────────────────────────────────────────
revision: str = "perf001_cash_flow_type"
down_revision: str | Sequence[str] | None = "lmt0_wealth_pre_cutover"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CASH_FLOW_VALUES = ("external_in", "external_out", "internal", "none")


def _values_in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("brokerage_transaction") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cash_flow_type",
                sa.String(length=16),
                nullable=False,
                server_default="none",
            )
        )
        batch_op.create_check_constraint(
            "ck_brokerage_tx_cash_flow_type",
            f"cash_flow_type IN ({_values_in(_CASH_FLOW_VALUES)})",
        )
        batch_op.create_index(
            "ix_brokerage_transaction_cash_flow_type",
            ["cash_flow_type"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM brokerage_transaction"
            " WHERE cash_flow_type IS NOT NULL AND cash_flow_type != 'none'"
        )
    ).scalar_one()
    if rows:
        raise RuntimeError(
            f"refusing to downgrade: {rows} brokerage_transaction rows have a "
            "non-'none' cash_flow_type; reset them to 'none' first or accept "
            "the data loss explicitly by stamping past this revision."
        )

    with op.batch_alter_table("brokerage_transaction") as batch_op:
        batch_op.drop_index("ix_brokerage_transaction_cash_flow_type")
        batch_op.drop_constraint(
            "ck_brokerage_tx_cash_flow_type", type_="check"
        )
        batch_op.drop_column("cash_flow_type")
