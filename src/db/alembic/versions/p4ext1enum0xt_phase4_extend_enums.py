"""phase4 extend Broker and AccountType CHECK constraints

Revision ID: p4ext1enum0xt
Revises: 0dfd3fb13224
Create Date: 2026-05-07 13:00:00.000000

Adds 4 new ``Broker`` enum values (franklin_templeton, nw_mutual, fg_annuity,
gsk_pension) and 1 new ``AccountType`` value (other) so the Phase-4 adapters can
write ``Account`` rows for non-broker institutions (FT mutual fund, NW Mutual
whole-life, F&G annuity, GSK pension).

SQLite cannot ALTER a CHECK constraint in place; ``batch_alter_table`` recopies
the table and recreates constraints. No row rewrites are needed beyond the
copy.

Downgrade refuses to run if any row uses the newly-added enum values — silent
data loss is worse than a loud failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "p4ext1enum0xt"
down_revision: str | None = "0dfd3fb13224"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_BROKERS = ("etrade", "schwab", "vanguard", "fidelity")
_NEW_BROKERS = (
    *_OLD_BROKERS,
    "franklin_templeton",
    "nw_mutual",
    "fg_annuity",
    "gsk_pension",
)

_OLD_ACCOUNT_TYPES = (
    "taxable", "joint", "roth_ira", "trad_ira", "401k", "403b", "hsa", "529",
    "tod", "brokeragelink", "rsu",
)
_NEW_ACCOUNT_TYPES = (*_OLD_ACCOUNT_TYPES, "other")


def _values_in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("account") as batch_op:
        batch_op.drop_constraint("ck_account_broker", type_="check")
        batch_op.drop_constraint("ck_account_type", type_="check")
        batch_op.create_check_constraint(
            "ck_account_broker", f"broker IN ({_values_in(_NEW_BROKERS)})"
        )
        batch_op.create_check_constraint(
            "ck_account_type",
            f"account_type IN ({_values_in(_NEW_ACCOUNT_TYPES)})",
        )


def downgrade() -> None:
    bind = op.get_bind()
    new_brokers = tuple(b for b in _NEW_BROKERS if b not in _OLD_BROKERS)
    new_types = tuple(t for t in _NEW_ACCOUNT_TYPES if t not in _OLD_ACCOUNT_TYPES)
    if new_brokers:
        rows = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM account WHERE broker IN"
                f" ({_values_in(new_brokers)})"
            )
        ).scalar_one()
        if rows:
            raise RuntimeError(
                f"refusing to downgrade: {rows} account rows still use broker IN"
                f" ({new_brokers}); reassign or delete them first"
            )
    if new_types:
        rows = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM account WHERE account_type IN"
                f" ({_values_in(new_types)})"
            )
        ).scalar_one()
        if rows:
            raise RuntimeError(
                f"refusing to downgrade: {rows} account rows still use account_type"
                f" IN ({new_types}); reassign or delete them first"
            )

    with op.batch_alter_table("account") as batch_op:
        batch_op.drop_constraint("ck_account_broker", type_="check")
        batch_op.drop_constraint("ck_account_type", type_="check")
        batch_op.create_check_constraint(
            "ck_account_broker", f"broker IN ({_values_in(_OLD_BROKERS)})"
        )
        batch_op.create_check_constraint(
            "ck_account_type",
            f"account_type IN ({_values_in(_OLD_ACCOUNT_TYPES)})",
        )
