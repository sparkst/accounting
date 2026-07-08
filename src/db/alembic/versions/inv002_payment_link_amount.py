"""invoice.payment_link_amount column

Revision ID: inv002_payment_link_amount
Revises: alr01_alert_dispatch_payload
Create Date: 2026-07-07 00:00:02.000000

REQ-FIX-INV-002: new invariant — the three persisted payment-link fields
(``payment_link_url``, ``payment_link_id``, and this new
``payment_link_amount``) are either all set (link active at the recorded
amount) or all NULL. ``payment_link_amount`` records ``invoice.total`` at the
moment the link was created so ``create_payment_link`` can verify reuse is
still valid (amount unchanged) instead of blindly reusing a stale link after
a total-changing PATCH.

Additive, nullable column — no existing row is rewritten; historical
``sent`` invoices simply have ``payment_link_amount IS NULL`` until their
link is next touched (created or cleared), same backward-compatibility
posture as ``4cbdb23e658f_add_payment_link_and_send_tracking_``.

Downgrade drops the column — a real reversal, no protected-table rows lost.

Cross-workstream migration ledger note (plan
docs/superpowers/plans/2026-07-07-remediation-feature-program.md
§Migration ledger): the ledger lists this revision chaining onto
``tax001_shopify_payout_backfill``. This revision instead chains directly
onto the actual current head (``alr01_alert_dispatch_payload``) per explicit
task instruction; the backfill migration (owned by a separate agent) must
rebase its own ``down_revision`` onto THIS revision id
(``inv002_payment_link_amount``), not the reverse, to keep the chain linear.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "inv002_payment_link_amount"
down_revision: str | None = "alr01_alert_dispatch_payload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "payment_link_amount",
                sa.Numeric(precision=12, scale=2),
                nullable=True,
                comment=(
                    "invoice.total at the moment the current payment_link was "
                    "created; reuse is only valid when this equals the current "
                    "total"
                ),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.drop_column("payment_link_amount")
