"""alert_dispatch gains payload_json + delivery_channel

Revision ID: alr01_alert_dispatch_payload
Revises: pld05_expected_account_ignored_status
Create Date: 2026-07-07 00:00:01.000000

REQ-FIX-ALR-002: persist the exact payload handed to the webhook client so a
row recorded ``status='failed'`` can be replayed byte-for-byte by the
failed-row sweep instead of the alert being lost forever. ``delivery_channel``
discriminates ``n8n_webhook`` (swept) from ``resend_email`` (never swept —
Resend emitters regenerate fresh content on their own timer) so the sweep
predicate doesn't have to infer channel from payload nullability.

Both columns are additive and nullable — ``alert_dispatch`` is not a protected
table, but existing rows are never rewritten or dropped. Every NEW write path
(EA dispatcher, balance dispatcher, digest pulse) sets both columns from this
revision forward; historical rows stay NULL/NULL and are treated as legacy
webhook rows by the sweep's explicit ``delivery_channel IS NULL`` arm.

Downgrade drops both columns — a real reversal, no protected-table rows lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "alr01_alert_dispatch_payload"
down_revision: str | None = "pld05_expected_account_ignored_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("alert_dispatch") as batch_op:
        batch_op.add_column(sa.Column("payload_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("delivery_channel", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("alert_dispatch") as batch_op:
        batch_op.drop_column("delivery_channel")
        batch_op.drop_column("payload_json")
