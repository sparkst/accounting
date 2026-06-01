"""plaid tx: account.payment_method + chase/depository checks

Revision ID: 0d372cdb13df
Revises: perf001_cash_flow_type
Create Date: 2026-05-31 18:57:40.560838

Adds ``account.payment_method`` (nullable String(64)) — the join key used by
the Plaid transaction-sync adapter to stamp entity on incoming Plaid rows and
to supersede / skip overlapping bank-CSV rows.

Also extends the broker and account_type CHECK constraints to include the
Plaid-sourced depository values added in REQ-PT-001 / REQ-PT-017:
  broker:       + 'chase'
  account_type: + 'checking', 'savings'

SQLite cannot ALTER a CHECK constraint in place; ``batch_alter_table``
recopies the table and recreates constraints.  No row rewrites are needed
beyond the copy.

Downgrade refuses to run if any row uses the newly-added enum values — silent
data loss is worse than a loud failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0d372cdb13df"
down_revision: str | Sequence[str] | None = "perf001_cash_flow_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── CHECK constraint value lists ──────────────────────────────────────────────

_OLD_BROKERS = (
    "etrade", "schwab", "vanguard", "fidelity",
    "franklin_templeton", "nw_mutual", "fg_annuity", "gsk_pension",
)
_NEW_BROKERS = (*_OLD_BROKERS, "chase")

_OLD_ACCOUNT_TYPES = (
    "taxable", "joint", "roth_ira", "trad_ira", "401k", "403b", "hsa", "529",
    "tod", "brokeragelink", "rsu", "other",
)
_NEW_ACCOUNT_TYPES = (*_OLD_ACCOUNT_TYPES, "checking", "savings")


def _values_in(values: Sequence[str]) -> str:
    # Used ONLY for DDL CHECK-constraint strings (not DML). f-string
    # interpolation of literals is safe inside a CHECK constraint definition.
    # For DML queries (e.g. the COUNT(*) guards in downgrade()) use
    # sa.bindparam(expanding=True) instead — never interpolate into a WHERE.
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("account") as batch_op:
        # 1. Add the new column
        batch_op.add_column(
            sa.Column(
                "payment_method",
                sa.String(length=64),
                nullable=True,
                comment=(
                    "Label joining this account to register rows "
                    "(e.g. 'Chase ****1234'). "
                    "Join key for Plaid entity-stamp, CSV supersede, and CSV-skip."
                ),
            )
        )
        # 2. Extend the broker CHECK constraint to include 'chase'
        batch_op.drop_constraint("ck_account_broker", type_="check")
        batch_op.create_check_constraint(
            "ck_account_broker",
            f"broker IN ({_values_in(_NEW_BROKERS)})",
        )
        # 3. Extend the account_type CHECK constraint to include 'checking' + 'savings'
        batch_op.drop_constraint("ck_account_type", type_="check")
        batch_op.create_check_constraint(
            "ck_account_type",
            f"account_type IN ({_values_in(_NEW_ACCOUNT_TYPES)})",
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Guard: refuse if any row uses the newly-added broker values. Use a
    # parameterized expanding bindparam rather than f-string interpolation inside
    # sa.text() — behavior is identical (the values are hardcoded enum strings)
    # but the pattern is injection-safe by construction.
    new_brokers = tuple(b for b in _NEW_BROKERS if b not in _OLD_BROKERS)
    if new_brokers:
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

    # Guard: refuse if any row uses the newly-added account_type values.
    new_types = tuple(t for t in _NEW_ACCOUNT_TYPES if t not in _OLD_ACCOUNT_TYPES)
    if new_types:
        rows = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM account WHERE account_type IN :vals"
            ).bindparams(sa.bindparam("vals", value=list(new_types), expanding=True))
        ).scalar_one()
        if rows:
            raise RuntimeError(
                f"refusing to downgrade: {rows} account row(s) still use account_type IN"
                f" {new_types}; reassign or delete them first"
            )

    with op.batch_alter_table("account") as batch_op:
        # Reverse order: restore old CHECK constraints, then drop the column
        batch_op.drop_constraint("ck_account_broker", type_="check")
        batch_op.create_check_constraint(
            "ck_account_broker",
            f"broker IN ({_values_in(_OLD_BROKERS)})",
        )
        batch_op.drop_constraint("ck_account_type", type_="check")
        batch_op.create_check_constraint(
            "ck_account_type",
            f"account_type IN ({_values_in(_OLD_ACCOUNT_TYPES)})",
        )
        batch_op.drop_column("payment_method")
