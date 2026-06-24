"""add north_american to the Broker CHECK constraint

Revision ID: na_iul_01
Revises: al0_add_alert_dispatch
Create Date: 2026-06-24 16:00:00.000000

Adds one new ``Broker`` enum value (``north_american``) so the North American
Builder Plus IUL (indexed universal life) policy can write ``Account`` /
``AccountBalanceSnapshot`` rows through the same Phase-4 carrier machinery used
by NW Mutual, F&G, and the GSK pension.

SQLite cannot ALTER a CHECK constraint in place; ``batch_alter_table`` recopies
the table and recreates the constraint. No row rewrites are needed beyond the
copy.

Downgrade refuses to run if any row already uses ``north_american`` — silent
data loss is worse than a loud failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "na_iul_01"
down_revision: str | None = "al0_add_alert_dispatch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Full current set (etrade..other) as established by p4ext1enum0xt + p5cards01.
_OLD_BROKERS = (
    "etrade",
    "schwab",
    "vanguard",
    "fidelity",
    "franklin_templeton",
    "nw_mutual",
    "fg_annuity",
    "gsk_pension",
    "chase",
    "amex",
    "other",
)
_NEW_BROKERS = (
    "etrade",
    "schwab",
    "vanguard",
    "fidelity",
    "franklin_templeton",
    "nw_mutual",
    "fg_annuity",
    "gsk_pension",
    "north_american",
    "chase",
    "amex",
    "other",
)


def _values_in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("account") as batch_op:
        batch_op.drop_constraint("ck_account_broker", type_="check")
        batch_op.create_check_constraint(
            "ck_account_broker", f"broker IN ({_values_in(_NEW_BROKERS)})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT COUNT(*) FROM account WHERE broker = 'north_american'")
    ).scalar_one()
    if rows:
        raise RuntimeError(
            f"refusing to downgrade: {rows} account row(s) still use broker"
            " 'north_american'; reassign or delete them first"
        )

    with op.batch_alter_table("account") as batch_op:
        batch_op.drop_constraint("ck_account_broker", type_="check")
        batch_op.create_check_constraint(
            "ck_account_broker", f"broker IN ({_values_in(_OLD_BROKERS)})"
        )
