"""Brokerage ORM models — isolated from the Transaction register.

REQ-005a..g: Account registry, transactions, position snapshots, realized G/L.
Phase 1 goal: net worth + performance tracking. P&L flow deferred to Phase 2.

Decimal convention:
- quantity, price: Numeric(18, 8) — fractional shares can have 8+ places
- amount, fees, market_value, cost_basis: Numeric(12, 2) — currency
- amount sign matches CLAUDE.md project convention: positive in / negative out
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from src.models.base import Base
from src.models.enums import (
    AccountType,
    Broker,
    BrokerageTxStatus,
    CanonicalAction,
    Entity,
    GainLossTerm,
)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ── Reusable enum value joiners for CHECK constraints ────────────────────
_BROKER_VALUES = "', '".join(b.value for b in Broker)
_ACCOUNT_TYPE_VALUES = "', '".join(a.value for a in AccountType)
_CANONICAL_ACTION_VALUES = "', '".join(c.value for c in CanonicalAction)
_TERM_VALUES = "', '".join(t.value for t in GainLossTerm)
_TX_STATUS_VALUES = "', '".join(s.value for s in BrokerageTxStatus)
_ENTITY_VALUES = "', '".join(e.value for e in Entity)


class Account(Base):
    """One brokerage account. UNIQUE on (broker, account_number)."""

    __tablename__ = "account"

    __table_args__ = (
        UniqueConstraint("broker", "account_number", name="uq_account_broker_number"),
        UniqueConstraint(
            "plaid_item_id", "plaid_account_id", name="uq_account_plaid_link"
        ),
        CheckConstraint(
            f"broker IN ('{_BROKER_VALUES}')",
            name="ck_account_broker",
        ),
        CheckConstraint(
            f"account_type IN ('{_ACCOUNT_TYPE_VALUES}')",
            name="ck_account_type",
        ),
        CheckConstraint(
            f"entity IN ('{_ENTITY_VALUES}')",
            name="ck_account_entity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    broker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    account_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Entity.PERSONAL.value
    )
    tax_sheltered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    parent_account_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("account.id"),
        nullable=True,
        comment="Self-FK: BrokerageLink → MS 401k plan wrapper",
    )
    is_plan_wrapper: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True for plan-only accounts whose transactions should not flow to P&L",
    )
    beneficiary: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Free-text — e.g. 'Aiden', 'Emerson'"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # REQ-025: Plaid linkage. NULL when account is CSV/XLSX-only. UNIQUE on
    # (plaid_item_id, plaid_account_id) at the table-args level prevents two
    # Account rows pointing at the same Plaid account.
    plaid_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plaid_item.id"), nullable=True
    )
    plaid_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    parent: Mapped["Account | None"] = relationship(
        "Account", remote_side="Account.id", backref="children"
    )
    transactions: Mapped[list["BrokerageTransaction"]] = relationship(
        "BrokerageTransaction", back_populates="account", cascade="all, delete-orphan"
    )
    positions: Mapped[list["PositionSnapshot"]] = relationship(
        "PositionSnapshot", back_populates="account", cascade="all, delete-orphan"
    )
    realized_lots: Mapped[list["RealizedGainLoss"]] = relationship(
        "RealizedGainLoss", back_populates="account", cascade="all, delete-orphan"
    )


class BrokerageTransaction(Base):
    """One brokerage transaction. UNIQUE on (account_id, source_row_hash)."""

    __tablename__ = "brokerage_transaction"

    __table_args__ = (
        UniqueConstraint(
            "account_id", "source_row_hash", name="uq_brokerage_tx_dedup"
        ),
        CheckConstraint(
            f"canonical_action IN ('{_CANONICAL_ACTION_VALUES}')",
            name="ck_brokerage_tx_canonical_action",
        ),
        CheckConstraint(
            f"status IN ('{_TX_STATUS_VALUES}')",
            name="ck_brokerage_tx_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("account.id"), nullable=False, index=True
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    settlement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Broker-native action string"
    )
    canonical_action: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    cusip: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[Any] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=True
    )
    price: Mapped[Any] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=True
    )
    amount: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=True,
        comment="Signed: positive = cash in, negative = cash out",
    )
    commission: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    fees: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    paired_transaction_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("brokerage_transaction.id"),
        nullable=True,
        comment="Links dividend ↔ reinvest pair",
    )
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True if this row was synthesized (e.g. E*TRADE single-row reinvest dividend partner)",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=BrokerageTxStatus.IMPORTED.value,
        server_default=BrokerageTxStatus.IMPORTED.value,
        index=True,
    )
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source_row_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_data: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    account: Mapped["Account"] = relationship("Account", back_populates="transactions")
    paired: Mapped["BrokerageTransaction | None"] = relationship(
        "BrokerageTransaction", remote_side="BrokerageTransaction.id"
    )


class PositionSnapshot(Base):
    """One holding row at a point in time. UNIQUE on (account_id, source_row_hash)."""

    __tablename__ = "position_snapshot"

    __table_args__ = (
        UniqueConstraint(
            "account_id", "source_row_hash", name="uq_position_snapshot_dedup"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("account.id"), nullable=False, index=True
    )
    as_of: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[Any] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=True
    )
    price: Mapped[Any] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=True
    )
    market_value: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    cost_basis: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    avg_cost_basis: Mapped[Any] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=True
    )
    unrealized_gain: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source_row_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_data: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    account: Mapped["Account"] = relationship("Account", back_populates="positions")


class RealizedGainLoss(Base):
    """One closed lot. UNIQUE on (account_id, source_row_hash)."""

    __tablename__ = "realized_gain_loss"

    __table_args__ = (
        UniqueConstraint(
            "account_id", "source_row_hash", name="uq_realized_gl_dedup"
        ),
        CheckConstraint(
            f"term IN ('{_TERM_VALUES}') OR term IS NULL",
            name="ck_realized_gl_term",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("account.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    quantity: Mapped[Any] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=False
    )
    proceeds: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=False
    )
    cost_basis: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=False
    )
    unadjusted_cost_basis: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    gain_loss: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=False
    )
    lt_gain_loss: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    st_gain_loss: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    term: Mapped[str | None] = mapped_column(String(8), nullable=True)
    wash_sale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    disallowed_loss: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True), nullable=True
    )
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source_row_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_data: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    account: Mapped["Account"] = relationship("Account", back_populates="realized_lots")


__all__ = [
    "Account",
    "BrokerageTransaction",
    "PositionSnapshot",
    "RealizedGainLoss",
]
