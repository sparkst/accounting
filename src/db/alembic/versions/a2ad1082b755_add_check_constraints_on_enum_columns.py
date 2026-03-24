"""add CHECK constraints on enum columns

Revision ID: a2ad1082b755
Revises: 1c8d9ab67214
Create Date: 2026-03-24 14:23:52.302780

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2ad1082b755'
down_revision: Union[str, Sequence[str], None] = '1c8d9ab67214'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ENTITY_VALUES = "', '".join(["sparkry", "blackline", "personal"])
_STATUS_VALUES = "', '".join([
    "auto_classified", "needs_review", "confirmed", "split_parent", "rejected"
])
_DIRECTION_VALUES = "', '".join(["income", "expense", "transfer", "reimbursable"])
_TAX_CATEGORY_VALUES = "', '".join([
    "ADVERTISING", "CAR_AND_TRUCK", "CONTRACT_LABOR", "INSURANCE", "HEALTH_INSURANCE",
    "LEGAL_AND_PROFESSIONAL", "OFFICE_EXPENSE", "SUPPLIES", "TAXES_AND_LICENSES",
    "TRAVEL", "MEALS", "COGS", "CONSULTING_INCOME", "SUBSCRIPTION_INCOME",
    "SALES_INCOME", "WHOLESALE_INCOME", "REIMBURSABLE",
    "CHARITABLE_CASH", "CHARITABLE_STOCK", "MEDICAL", "STATE_LOCAL_TAX",
    "MORTGAGE_INTEREST", "INVESTMENT_INCOME", "PERSONAL_NON_DEDUCTIBLE",
    "CAPITAL_CONTRIBUTION", "OTHER_EXPENSE",
])


def upgrade() -> None:
    """Upgrade schema."""
    # Fix type changes detected by autogenerate
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.alter_column('amount_foreign',
               existing_type=sa.FLOAT(),
               type_=sa.Numeric(precision=18, scale=6),
               existing_nullable=True)
        batch_op.alter_column('exchange_rate',
               existing_type=sa.FLOAT(),
               type_=sa.Numeric(precision=18, scale=8),
               existing_nullable=True)

    # Add CHECK constraints on enum columns.
    # SQLite requires batch_alter_table to rebuild the table with new constraints.
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.create_check_constraint(
            'ck_transaction_entity',
            f"entity IN ('{_ENTITY_VALUES}') OR entity IS NULL",
        )
        batch_op.create_check_constraint(
            'ck_transaction_status',
            f"status IN ('{_STATUS_VALUES}')",
        )
        batch_op.create_check_constraint(
            'ck_transaction_direction',
            f"direction IN ('{_DIRECTION_VALUES}') OR direction IS NULL",
        )
        batch_op.create_check_constraint(
            'ck_transaction_tax_category',
            f"tax_category IN ('{_TAX_CATEGORY_VALUES}') OR tax_category IS NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.drop_constraint('ck_transaction_tax_category', type_='check')
        batch_op.drop_constraint('ck_transaction_direction', type_='check')
        batch_op.drop_constraint('ck_transaction_status', type_='check')
        batch_op.drop_constraint('ck_transaction_entity', type_='check')
        batch_op.alter_column('exchange_rate',
               existing_type=sa.Numeric(precision=18, scale=8),
               type_=sa.FLOAT(),
               existing_nullable=True)
        batch_op.alter_column('amount_foreign',
               existing_type=sa.Numeric(precision=18, scale=6),
               type_=sa.FLOAT(),
               existing_nullable=True)
