"""SQLAlchemy ORM models for the accounting system."""

from src.models.audit_event import AuditEvent
from src.models.base import Base

# P2-005: brokerage/plaid/history models were NOT registered here, so
# Base.metadata was incomplete whenever a test module happened to be
# collected before any module that imports them directly (e.g. running
# `pytest src/api/test_brokerage_routes.py` alone raised
# `NoReferencedTableError: ... account.plaid_item_id could not find table
# 'plaid_item'` because plaid.py's PlaidItem was never imported). Importing
# them here — alongside every other model package — makes Base.metadata
# complete regardless of import order / which test file pytest happens to
# collect first.
from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot, RealizedGainLoss
from src.models.history import (
    AccountAlias,
    AccountBalanceSnapshot,
    AccountTag,
    CostBasisLot,
    ExpectedAccount,
    HistoricalPrice,
    StockSplit,
)
from src.models.ingested_file import IngestedFile
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem
from src.models.tax_document import TaxDocument
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

__all__ = [
    "Base",
    "Transaction",
    "TaxDocument",
    "VendorRule",
    "IngestedFile",
    "AuditEvent",
    "IngestionLog",
    "Account",
    "BrokerageTransaction",
    "PositionSnapshot",
    "RealizedGainLoss",
    "HistoricalPrice",
    "AccountBalanceSnapshot",
    "ExpectedAccount",
    "AccountTag",
    "CostBasisLot",
    "StockSplit",
    "AccountAlias",
    "PlaidItem",
    "PlaidAccountBalanceSnapshot",
]
