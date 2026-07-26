"""plaid consolidation: plaid_item.scope ('register' | 'wealth')

Revision ID: pldcons01_plaid_item_scope
Revises: mca01_confirmed_by_widen_ar_vision
Create Date: 2026-07-25

REQ-PC-B1 (Plaid consolidation — box becomes the sole Plaid owner): every
``plaid_item`` row gains a ``scope`` discriminator:

  * ``register`` (default) — the pre-consolidation behavior: transactions feed
    the cash-basis register, balance snapshots map to local Account rows, and
    unmapped accounts surface as ``expected_account`` rows.
  * ``wealth`` — imported wealth-side Items (E*TRADE, Vanguard, BofA, Citi,
    PenFed, …). Balances/holdings are pushed to the wealth D1 only; a
    wealth-scope Item must never produce register transactions, local
    snapshot mappings, or expected_account rows.

CHECK constraint values are the enum VALUES ('register', 'wealth') — matching
``src.models.plaid.PLAID_ITEM_SCOPES``.

SQLite cannot ALTER constraints in place; ``batch_alter_table`` recopies the
table (named constraints from the model-era CREATE survive the copy).

Downgrade refuses to run while any row still carries ``scope='wealth'`` —
silently dropping the column would revert those Items to register scope and
the next transactions sync would try to ingest wealth-institution feeds into
the register. Loud failure over silent data corruption.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "pldcons01_plaid_item_scope"
down_revision: str | Sequence[str] | None = "mca01_confirmed_by_widen_ar_vision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enum VALUES (mirrors src.models.plaid.PLAID_ITEM_SCOPES).
_SCOPES = ("register", "wealth")


def upgrade() -> None:
    with op.batch_alter_table("plaid_item") as batch_op:
        batch_op.add_column(
            sa.Column(
                "scope",
                sa.String(length=16),
                nullable=False,
                server_default="register",
                comment=(
                    "'register' feeds the cash-basis register; 'wealth' pushes "
                    "balances/holdings to the wealth D1 only (never the register)."
                ),
            )
        )
        batch_op.create_check_constraint(
            "ck_plaid_item_scope",
            f"scope IN ({', '.join(repr(s) for s in _SCOPES)})",
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Guard: refuse while wealth-scope rows exist. Dropping the column would
    # silently revert them to register scope, letting the transactions sync
    # ingest wealth-institution feeds into the register.
    rows = bind.execute(
        sa.text("SELECT COUNT(*) FROM plaid_item WHERE scope = :s").bindparams(
            s="wealth"
        )
    ).scalar_one()
    if rows:
        raise RuntimeError(
            f"refusing to downgrade: {rows} plaid_item row(s) still have "
            "scope='wealth'; disconnect or delete them first"
        )

    with op.batch_alter_table("plaid_item") as batch_op:
        batch_op.drop_constraint("ck_plaid_item_scope", type_="check")
        batch_op.drop_column("scope")
