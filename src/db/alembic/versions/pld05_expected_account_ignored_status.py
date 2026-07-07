"""expected_account CHECK gains 'ignored' status

Revision ID: pld05_expected_account_ignored_status
Revises: na_iul_01
Create Date: 2026-07-07 00:00:00.000000

REQ-FIX-PLD-005: unmapped Plaid accounts the user has explicitly triaged as
"never mine" need an ignore-list so they stop counting as unmapped in the
sync log detail and the daily pulse. Rather than a new table (duplicating the
natural key + join logic that ``expected_account`` already has), this adds one
CHECK-constraint value: ``'ignored'``.

SQLite cannot ALTER a CHECK constraint in place; ``batch_alter_table`` recopies
the table and recreates the constraint. No row rewrites are needed for upgrade.

Downgrade flips any ``ignored`` rows back to ``unconfirmed`` (UPDATE, never a
DELETE — CLAUDE.md "never delete transactions" extends to every status-only
row in this codebase) before restoring the old constraint, so a downgrade
never fails on a CHECK violation from existing ``ignored`` rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "pld05_expected_account_ignored_status"
down_revision: str | None = "na_iul_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUSES = ("active", "closed", "unconfirmed")
_NEW_STATUSES = ("active", "closed", "unconfirmed", "ignored")


def _values_in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("expected_account") as batch_op:
        batch_op.drop_constraint("ck_expected_account_status", type_="check")
        batch_op.create_check_constraint(
            "ck_expected_account_status", f"status IN ({_values_in(_NEW_STATUSES)})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    # UPDATE, never DELETE — flip ignored rows back to unconfirmed so the old
    # (narrower) CHECK constraint can be restored without data loss.
    bind.execute(
        sa.text(
            "UPDATE expected_account SET status = 'unconfirmed' WHERE status = 'ignored'"
        )
    )
    with op.batch_alter_table("expected_account") as batch_op:
        batch_op.drop_constraint("ck_expected_account_status", type_="check")
        batch_op.create_check_constraint(
            "ck_expected_account_status", f"status IN ({_values_in(_OLD_STATUSES)})"
        )
