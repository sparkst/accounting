"""Build the planning-loader fixture SQLite from scratch.

Idempotent: drops + recreates the file at tests/fixtures/planning/accounting.fixture.db.
Run with: `python -m tests.fixtures.planning.build_fixture_db`.

Contents:
  - 3 Accounts: 1 taxable brokerage, 1 trad_ira, 1 checking
  - 12 months of AccountBalanceSnapshot rows (one per account per month)
  - 12 months of Transaction rows hitting personal-entity expenses,
    sparkry-entity income, and a few personal-entity income credits

Totals are deterministic and known so test assertions can be exact.

Schema findings (from Step 0 inspection):
  - Account.account_type: plain string column with lowercase StrEnum values
    ("taxable", "trad_ira", "checking", etc.) — not uppercase "TAXABLE"/"IRA"
  - Account requires: broker (NOT NULL), account_number (NOT NULL)
  - Account.account_name is the display name field (not "name")
  - AccountBalanceSnapshot date column is "as_of" (not "snapshot_date")
  - AccountBalanceSnapshot requires: raw_account_name, source, source_row_hash
  - Transaction.date is String(10) ISO date "YYYY-MM-DD", not a Date column
  - Transaction.source_hash is required (unique NOT NULL)
  - Transaction.raw_data is required (NOT NULL JSON)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sys
from decimal import Decimal
from pathlib import Path

# Ensure repo root is on sys.path so `src.*` imports work when this script
# is run directly (python tests/fixtures/planning/build_fixture_db.py).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

# Import Base and all models so SQLAlchemy metadata sees them
from src.db.connection import Base  # noqa: E402
from src.models.brokerage import Account  # noqa: E402, F401
from src.models.history import AccountBalanceSnapshot  # noqa: E402, F401
from src.models.transaction import Transaction  # noqa: E402, F401

FIXTURE_PATH = Path(__file__).parent / "accounting.fixture.db"


def _hash(source: str, key: str) -> str:
    """Generate a deterministic source_row_hash."""
    return hashlib.sha256(f"{source}:{key}".encode()).hexdigest()


def build() -> None:
    if FIXTURE_PATH.exists():
        FIXTURE_PATH.unlink()

    engine = create_engine(f"sqlite:///{FIXTURE_PATH}")
    Base.metadata.create_all(engine)

    with Session(engine) as s:
        # --- Accounts ---
        # AccountType values are lowercase StrEnum: "taxable", "trad_ira", "checking"
        # broker and account_number are required NOT NULL columns
        taxable = Account(
            account_name="Schwab Brokerage",
            account_type="taxable",
            broker="schwab",
            account_number="fixture-taxable-001",
        )
        ira = Account(
            account_name="Schwab IRA",
            account_type="trad_ira",
            broker="schwab",
            account_number="fixture-ira-001",
        )
        checking = Account(
            account_name="Personal Checking",
            account_type="checking",
            broker="chase",
            account_number="fixture-checking-001",
        )
        s.add_all([taxable, ira, checking])
        s.flush()  # get IDs assigned

        # --- 12 months of AccountBalanceSnapshot rows ---
        # Column is "as_of" (not "snapshot_date")
        # Required fields: raw_account_name, source, source_row_hash
        today = dt.date(2026, 6, 1)
        for months_ago in range(0, 12):
            # Step back one month at a time (approximate; avoids date arithmetic edge cases)
            snap_date = dt.date(
                today.year if today.month - months_ago > 0
                else today.year - ((months_ago - today.month) // 12 + 1),
                ((today.month - months_ago - 1) % 12) + 1,
                1,
            )
            for acct, raw_name, balance in [
                (taxable, "Schwab Brokerage", Decimal("6300000.00")),
                (ira, "Schwab IRA", Decimal("1500000.00")),
                (checking, "Personal Checking", Decimal("50000.00")),
            ]:
                row_key = f"{acct.account_number}:{snap_date.isoformat()}"
                s.add(
                    AccountBalanceSnapshot(
                        account_id=acct.id,
                        raw_account_name=raw_name,
                        as_of=snap_date,
                        balance=balance,
                        source="fixture",
                        source_row_hash=_hash("fixture", row_key),
                    )
                )

        # --- 12 months of Transactions ---
        # Known annual totals:
        #   personal expense: 12 × $20k = $240k (TTM spend)
        #   sparkry income:   12 × $26,666.67 = ~$320k (TTM biz income)
        #   personal income:  12 × $6,666.67 = ~$80k (TTM personal credits)
        #
        # Transaction.date is String(10) ISO date, not a Date column.
        # Transaction.source_hash is required (unique). Transaction.raw_data
        # is required (NOT NULL JSON).
        for months_ago in range(12):
            tx_date = (today - dt.timedelta(days=months_ago * 30)).isoformat()

            # Personal expense
            exp_key = f"expense:{months_ago}"
            s.add(Transaction(
                date=tx_date,
                amount=Decimal("-20000.00"),
                direction="expense",
                entity="personal",
                description=f"month-{months_ago} personal expense",
                source="fixture",
                source_hash=_hash("fixture-expense", exp_key),
                raw_data={"fixture": True, "month": months_ago, "type": "expense"},
            ))

            # Sparkry income
            biz_key = f"sparkry-income:{months_ago}"
            s.add(Transaction(
                date=tx_date,
                amount=Decimal("26666.67"),
                direction="income",
                entity="sparkry",
                description=f"month-{months_ago} sparkry income",
                source="fixture",
                source_hash=_hash("fixture-biz", biz_key),
                raw_data={"fixture": True, "month": months_ago, "type": "sparkry_income"},
            ))

            # Personal income
            inc_key = f"personal-income:{months_ago}"
            s.add(Transaction(
                date=tx_date,
                amount=Decimal("6666.67"),
                direction="income",
                entity="personal",
                description=f"month-{months_ago} personal income",
                source="fixture",
                source_hash=_hash("fixture-personal-inc", inc_key),
                raw_data={"fixture": True, "month": months_ago, "type": "personal_income"},
            ))

        s.commit()

    print(f"Built fixture: {FIXTURE_PATH}")
    print(f"  Size: {FIXTURE_PATH.stat().st_size:,} bytes")


if __name__ == "__main__":
    build()
