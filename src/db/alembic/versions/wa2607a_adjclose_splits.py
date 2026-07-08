"""historical_price.adj_close + stock_split table

Revision ID: wa2607a_adjclose_splits
Revises: tax001_shopify_payout_backfill
Create Date: 2026-07-07 00:00:04.000000

REQ-FIX-WLT-001/002 (wealth design §1.4, §2). Additive:

* ``historical_price.adj_close`` — total-return-capable adjusted close, NULL
  until backfill runs. A derived analytics column; raw ``close`` is untouched.
* new ``stock_split`` table — corporate split events keyed on (symbol, ex_date),
  ``ratio`` = post/pre. Populated from the real yfinance splits API.

Downgrade drops ``stock_split`` then drops ``adj_close`` via SQLite-safe batch
mode. The only data loss on downgrade is the derived ``adj_close`` column and
the split-events table — acceptable and documented (neither is audit data).

Cross-workstream migration ledger: chains onto the current head
``tax001_shopify_payout_backfill`` (WS2 tip), per the program ledger.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "wa2607a_adjclose_splits"
down_revision: str | None = "tax001_shopify_payout_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("historical_price") as batch_op:
        batch_op.add_column(
            sa.Column("adj_close", sa.Numeric(precision=18, scale=8), nullable=True)
        )

    op.create_table(
        "stock_split",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("ratio", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="yfinance",
        ),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "ex_date", name="pk_stock_split"),
    )
    op.create_index("ix_stock_split_symbol", "stock_split", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_stock_split_symbol", table_name="stock_split")
    op.drop_table("stock_split")
    with op.batch_alter_table("historical_price") as batch_op:
        batch_op.drop_column("adj_close")
