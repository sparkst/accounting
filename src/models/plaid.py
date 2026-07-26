"""Plaid Phase 1 ORM models — Items and Balance snapshots.

REQ-025..029: see ``requirements/current.md``.

Design notes:
- ``access_token_encrypted`` stores Fernet ciphertext (``src/utils/plaid_crypto.py``).
  On ``/item/remove``, this column is overwritten with the sentinel ``"REVOKED"`` so
  the ciphertext does not linger in SQLite freed pages or `.wal` snapshots.
- ``PlaidAccountBalanceSnapshot.current_balance`` stores Plaid's value AS-RETURNED.
  For ``plaid_account_type IN ('credit', 'loan')`` the value is positive (debt) and
  net-worth aggregation must NEGATE it. Sign normalization is NOT done at write time
  so the row remains round-trippable with ``raw_data``.
- ``UniqueConstraint(account_id, snapshot_date)`` is the idempotency key: a second
  cron run on the same day raises IntegrityError, which the per-row savepoint absorbs.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models.base import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


PLAID_ITEM_STATUSES = ("active", "disconnected", "pending_oauth", "abandoned")
PLAID_SYNC_STATUSES = ("ok", "error", "pending", "institution_down")
# REQ-PC-B1 (Plaid consolidation): what an Item's data feeds.
#   'register' — transactions land in the cash-basis register; balance snapshots
#                map to local Account rows and unmapped accounts surface as
#                ExpectedAccount rows (the pre-consolidation behavior).
#   'wealth'   — balances/holdings are pushed to the wealth D1 only. A wealth
#                Item must NEVER produce register transactions, local snapshot
#                mappings, or expected_account rows.
PLAID_ITEM_SCOPES = ("register", "wealth")
PLAID_ACCOUNT_TYPES = (
    "depository",
    "credit",
    "investment",
    "brokerage",
    "loan",
    "other",
)

# Plaid account types whose ``current_balance`` represents a LIABILITY (positive
# = amount owed). Net-worth aggregation negates these.
PLAID_LIABILITY_TYPES = frozenset({"credit", "loan"})

# Sentinel used to overwrite ``access_token_encrypted`` after disconnect. Avoids
# ciphertext lingering in SQLite freed pages / WAL snapshots / backup.
REVOKED_TOKEN_SENTINEL = "REVOKED"


class PlaidItem(Base):
    """One Plaid Item — i.e. one institution login.

    A single Item can hold many accounts. Plaid Items are reversible via
    ``/item/remove`` — disconnect frees the institution slot immediately.
    """

    __tablename__ = "plaid_item"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_plaid_item_item_id"),
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in PLAID_ITEM_STATUSES)})",
            name="ck_plaid_item_status",
        ),
        CheckConstraint(
            "last_sync_status IS NULL OR last_sync_status IN "
            f"({', '.join(repr(s) for s in PLAID_SYNC_STATUSES)})",
            name="ck_plaid_item_last_sync_status",
        ),
        CheckConstraint(
            f"scope IN ({', '.join(repr(s) for s in PLAID_ITEM_SCOPES)})",
            name="ck_plaid_item_scope",
        ),
        Index("ix_plaid_item_status", "status"),
        Index("ix_plaid_item_institution_id", "institution_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    institution_id: Mapped[str] = mapped_column(String(64), nullable=False)
    institution_name: Mapped[str] = mapped_column(String(128), nullable=False)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # ``cursor`` is reserved for /transactions/sync (Phase 2+); never used for
    # Balance/Investments. Kept here so the column exists when Phase 2 lands.
    cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_expiration_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state_nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state_nonce_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    # REQ-PC-B1: 'register' | 'wealth' — see PLAID_ITEM_SCOPES above.
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="register", server_default="register"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    def __repr__(self) -> str:
        return (
            f"<PlaidItem id={self.id[:8]} institution={self.institution_name!r}"
            f" status={self.status} last_sync={self.last_sync_at}>"
        )


class PlaidAccountBalanceSnapshot(Base):
    """Daily Plaid Balance snapshot — sibling to ``account_balance_snapshot``.

    Idempotency key is (account_id, snapshot_date). A second cron run on the same
    day collides on the UniqueConstraint; the per-row savepoint absorbs it and the
    run continues.
    """

    __tablename__ = "plaid_account_balance_snapshot"
    __table_args__ = (
        UniqueConstraint("account_id", "snapshot_date", name="uq_plaid_bal_snap_acct_date"),
        CheckConstraint(
            f"plaid_account_type IN ({', '.join(repr(t) for t in PLAID_ACCOUNT_TYPES)})",
            name="ck_plaid_bal_snap_account_type",
        ),
        Index("ix_plaid_bal_snap_account_id", "account_id"),
        Index("ix_plaid_bal_snap_snapshot_date", "snapshot_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("account.id"), nullable=False
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    plaid_account_type: Mapped[str] = mapped_column(String(24), nullable=False)
    plaid_account_subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_balance: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4, asdecimal=True), nullable=False
    )
    available_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=4, asdecimal=True), nullable=True
    )
    iso_currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    pulled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PlaidBalSnap account={self.account_id[:8]} date={self.snapshot_date}"
            f" type={self.plaid_account_type} bal={self.current_balance}>"
        )
