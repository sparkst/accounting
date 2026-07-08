"""History models — balance snapshots, EOD prices, expected accounts, lot basis.

These tables sit alongside the live brokerage models and support:
- Net-worth-over-time (``account_balance_snapshot``)
- Per-holding price history (``historical_price``)
- Account-coverage / "missing accounts" panel (``expected_account``)
- Lot-level cost basis ingested from Excel (``cost_basis_lot`` — read-only)

Decimal precision matches ``src/models/brokerage.py``: quantity/price use
``Numeric(18, 8)``; balance/cost_total/wash_sale_adj use ``Numeric(14, 2)``
(wider than brokerage's 12,2 because XLSX totals reach 8 figures).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# Status values for expected_account.status
# 'ignored' added by migration pld05_expected_account_ignored_status (REQ-FIX-PLD-005):
# the ignore-list mechanism for unmapped Plaid accounts the user never wants surfaced.
_EXPECTED_STATUS_VALUES = "'active', 'closed', 'unconfirmed', 'ignored'"


class HistoricalPrice(Base):
    """End-of-day price for a symbol on a given date.

    Composite PK on (symbol, trade_date). Source tracked so we know whether
    a row came from yfinance, Stooq, or an XLSX seed.
    """

    __tablename__ = "historical_price"
    __table_args__ = (
        Index("ix_historical_price_date", "trade_date"),
    )

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=False
    )
    # REQ-FIX-WLT-001: total-return-capable adjusted close. NULL when the source
    # (XLSX seed) has no adjusted series or backfill hasn't run yet. This is a
    # DERIVED analytics column — Yahoo restates it after every dividend/split, so
    # idempotent overwrites are sanctioned (raw ``close`` is never touched).
    adj_close: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=True
    )
    open: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=True
    )
    high: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=True
    )
    low: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=True
    )
    volume: Mapped[int | None] = mapped_column(nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="yfinance", server_default="yfinance"
    )
    ingested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class AccountBalanceSnapshot(Base):
    """Account balance at a point in time, sourced from XLSX import or computed.

    account_id is nullable because XLSX rows may reference accounts that don't
    exist yet (or never will, e.g. closed accounts). raw_account_name is the
    XLSX label preserved for audit and later reconciliation.
    """

    __tablename__ = "account_balance_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "as_of", "source", name="uq_acct_balance_snap_acct_date_src"
        ),
        UniqueConstraint(
            "raw_account_name",
            "as_of",
            "source",
            name="uq_acct_balance_snap_rawname_date_src",
        ),
        Index("ix_acct_balance_snap_as_of", "as_of"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("account.id"), nullable=True, index=True
    )
    raw_account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    balance: Mapped[Decimal] = mapped_column(
        Numeric(precision=14, scale=2, asdecimal=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_row_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    account = relationship("Account")


class ExpectedAccount(Base):
    """Manually-curated list of accounts the user expects to see in the system.

    Drives the "missing accounts" panel: any active expected_account whose
    resolved_account_id has no recent snapshot is flagged.
    """

    __tablename__ = "expected_account"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_EXPECTED_STATUS_VALUES})", name="ck_expected_account_status"
        ),
        UniqueConstraint(
            "institution",
            "account_name",
            "last_4",
            name="uq_expected_account_natural_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    institution: Mapped[str] = mapped_column(String(64), nullable=False)
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_4: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unconfirmed", server_default="unconfirmed"
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("account.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    resolved_account = relationship("Account")


class AccountTag(Base):
    """A single (account, tag) association.

    Composite PK on (account_id, tag) — an account holds the same tag at most
    once, but may have many tags. Tags are free-text but case-insensitive at
    query time (callers must normalise to lower-case before insert/lookup).
    """

    __tablename__ = "account_tag"
    __table_args__ = (
        Index("ix_account_tag_tag", "tag"),
    )

    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("account.id"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    account = relationship("Account")


class CostBasisLot(Base):
    """A single lot of a security with cost basis, ingested from XLSX (TD/SB).

    These are historical/reference rows — we never use them to mutate the live
    Position.cost_basis. They surface in a per-symbol "historical lots" view.
    """

    __tablename__ = "cost_basis_lot"
    __table_args__ = (
        UniqueConstraint("source_row_hash", name="uq_cost_basis_lot_row_hash"),
        Index("ix_cost_basis_lot_symbol", "symbol"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("account.id"), nullable=True, index=True
    )
    raw_account_name: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    security_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    open_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=False
    )
    cost_per_share: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True), nullable=False
    )
    cost_total: Mapped[Decimal] = mapped_column(
        Numeric(precision=14, scale=2, asdecimal=True), nullable=False
    )
    wash_sale_adj: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=14, scale=2, asdecimal=True), nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    account = relationship("Account")


class StockSplit(Base):
    """Corporate stock-split events, keyed on (symbol, ex_date).

    REQ-FIX-WLT-002: split-safe re-pricing. ``ratio`` is post/pre (a 2:1 forward
    split → ``2.000000``; a 1:10 reverse split → ``0.100000``). Populated from
    the real yfinance ``Ticker.splits`` API — **never** derived from a
    close/adj_close ratio (that ratio also embeds dividends and can't be
    separated). When no rows exist for a symbol the cumulative ratio is 1
    (today's behaviour); correctness improves monotonically as split data lands.
    """

    __tablename__ = "stock_split"
    __table_args__ = (
        Index("ix_stock_split_symbol", "symbol"),
    )

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ex_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ratio: Mapped[Decimal] = mapped_column(
        Numeric(precision=12, scale=6, asdecimal=True), nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="yfinance", server_default="yfinance"
    )
    ingested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class AccountAlias(Base):
    """Legacy XLSX ``raw_account_name`` → live ``account.id`` mapping.

    REQ-FIX-WLT-004: mirrors the sparkry-crm D1 ``account_alias`` schema so the
    local networth-history dedup can compute a *per-name* effective cutoff
    (earliest PositionSnapshot.as_of of the aliased account) instead of a single
    global cutoff. ``raw_account_name`` is stored **lowercased** (the PK) per the
    REQ-WD-009 key-casing contract.
    """

    __tablename__ = "account_alias"

    raw_account_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("account.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    account = relationship("Account")
