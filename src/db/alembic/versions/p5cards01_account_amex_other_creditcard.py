"""account: broker += amex/other, account_type += credit_card (Plaid cards)

Revision ID: p5cards01amex
Revises: 63c79e8be034
Create Date: 2026-06-07

Plaid go-live (Phase 5) links Amex + Chase business credit cards alongside the
Chase depository accounts. Extends the account CHECK constraints:
  broker:       + 'amex', 'other'
  account_type: + 'credit_card'

Mirrors 0d372cdb13df: SQLite cannot ALTER a CHECK in place, so batch_alter_table
recopies the table and recreates the constraints. No row rewrites. The downgrade
refuses to run if any row uses the newly-added values (loud failure beats silent
data loss).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "p5cards01amex"
down_revision: str | Sequence[str] | None = "63c79e8be034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_BROKERS = (
    "etrade", "schwab", "vanguard", "fidelity",
    "franklin_templeton", "nw_mutual", "fg_annuity", "gsk_pension", "chase",
)
_NEW_BROKERS = (*_OLD_BROKERS, "amex", "other")

_OLD_ACCOUNT_TYPES = (
    "taxable", "joint", "roth_ira", "trad_ira", "401k", "403b", "hsa", "529",
    "tod", "brokeragelink", "rsu", "checking", "savings", "other",
)
_NEW_ACCOUNT_TYPES = (*_OLD_ACCOUNT_TYPES, "credit_card")


def _values_in(values: Sequence[str]) -> str:
    # DDL CHECK-constraint string only (not DML) — literal interpolation is safe.
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("account") as batch_op:
        batch_op.drop_constraint("ck_account_broker", type_="check")
        batch_op.create_check_constraint(
            "ck_account_broker", f"broker IN ({_values_in(_NEW_BROKERS)})"
        )
        batch_op.drop_constraint("ck_account_type", type_="check")
        batch_op.create_check_constraint(
            "ck_account_type", f"account_type IN ({_values_in(_NEW_ACCOUNT_TYPES)})"
        )


def downgrade() -> None:
    bind = op.get_bind()

    new_brokers = tuple(b for b in _NEW_BROKERS if b not in _OLD_BROKERS)
    rows = bind.execute(
        sa.text("SELECT COUNT(*) FROM account WHERE broker IN :vals").bindparams(
            sa.bindparam("vals", value=list(new_brokers), expanding=True)
        )
    ).scalar_one()
    if rows:
        raise RuntimeError(
            f"refusing to downgrade: {rows} account row(s) still use broker IN"
            f" {new_brokers}; reassign or delete them first"
        )

    new_types = tuple(t for t in _NEW_ACCOUNT_TYPES if t not in _OLD_ACCOUNT_TYPES)
    rows = bind.execute(
        sa.text("SELECT COUNT(*) FROM account WHERE account_type IN :vals").bindparams(
            sa.bindparam("vals", value=list(new_types), expanding=True)
        )
    ).scalar_one()
    if rows:
        raise RuntimeError(
            f"refusing to downgrade: {rows} account row(s) still use account_type IN"
            f" {new_types}; reassign or delete them first"
        )

    with op.batch_alter_table("account") as batch_op:
        batch_op.drop_constraint("ck_account_broker", type_="check")
        batch_op.create_check_constraint(
            "ck_account_broker", f"broker IN ({_values_in(_OLD_BROKERS)})"
        )
        batch_op.drop_constraint("ck_account_type", type_="check")
        batch_op.create_check_constraint(
            "ck_account_type", f"account_type IN ({_values_in(_OLD_ACCOUNT_TYPES)})"
        )
