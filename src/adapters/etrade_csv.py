"""E*TRADE CSV adapter — ingests transactions and positions from E*TRADE exports.

REQ-005a..g: Per-broker CSV parser writing to isolated brokerage tables.

Source files (in a per-account folder):
    DownloadTxnHistory.csv     — transaction history (workhorse)
    PortfolioDownload.csv      — current positions (net-worth tracking)
    tradesdownload.csv         — SKIPPED (sign convention is opposite, data
                                  duplicates DownloadTxnHistory)
    *.pdf                      — 1099 forms etc., not handled here

DownloadTxnHistory.csv layout (real example):
    Row 1: "All Transactions Activity Types"
    Row 2: blank
    Row 3: "Account Activity for Cap 1(-6084) -6354 from 2024-05-03 to 2026-05-03"
    Row 4: blank
    Row 5: "Total:,-40452.83"
    Row 6: blank
    Row 7: header  ("Activity/Trade Date,Transaction Date,Settlement Date,
                    Activity Type,Description,Symbol,Cusip,Quantity #,
                    Price $,Amount $,Commission,Category,Note")
    Row 8+: data

Account discovery: parse last numeric token before " from " in line 3.
    "Cap 1(-6084) -6354" → account_number "6354"

Action mapping (case-sensitive Activity Type):
    Bought                  → buy
    Sold                    → sell
    Dividend                → dividend_ordinary
    Qualified Dividend      → dividend_qualified
    Dividend Reinvestment   → reinvest (buy side; **synthesizes** a paired
                                          ordinary-dividend row)
    Interest / Interest Income → interest
    Stock Split             → stock_split
    Transfer / Wire / Direct Debit / Online Transfer → transfer
    Adjustment / Reorganization → other
    else                    → other
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from src.adapters.base import AdapterResult, BaseAdapter
from src.adapters.brokerage_csv_helpers import (
    compute_brokerage_row_hash,
    compute_position_row_hash,
    find_header_row,
    parse_currency,
    parse_date_flexible,
    parse_quantity,
    read_csv_tolerant,
)
from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot
from src.models.enums import (
    AccountType,
    Broker,
    BrokerageTxStatus,
    CanonicalAction,
    Entity,
    IngestionStatus,
    Source,
)
from src.models.ingestion_log import IngestionLog

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

TRANSACTIONS_FILENAME = "DownloadTxnHistory.csv"
POSITIONS_FILENAME = "PortfolioDownload.csv"
SKIPPED_FILENAME = "tradesdownload.csv"

_TXN_REQUIRED_COLS = {"Activity/Trade Date", "Activity Type"}
_POS_REQUIRED_COLS = {"Symbol", "Quantity", "Last Price $"}

# TASK-11: trailing rows like "Generated at May 4 2026 02:47 PM ET,,,,,,,,,"
# must not be ingested as positions. Real tickers are short alphanumeric (with
# an optional "**" suffix that some E*TRADE exports use for footnoted symbols),
# so any "symbol" cell that doesn't match this shape is treated as metadata.
_TICKER_RE = re.compile(r"^[A-Z0-9]{1,5}([.-][A-Z0-9]{1,3})?(\*\*)?$")

# REQ-FIX-WLT-003: the trailing footer row stamps the export time, e.g.
# "Generated at May 4 2026 02:47 PM ET". We parse the month-name + day + year
# out of it to derive the snapshot `as_of` (priority 2 in the ladder) — a
# dateutil-free parse using an explicit month-name map.
_GENERATED_AT_RE = re.compile(
    r"Generated at\s+([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
_MONTH_NAMES: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_generated_at_date(rows: list[list[str]]) -> date | None:
    """Scan raw CSV rows for a ``Generated at <Month> <day> <year> ...`` footer.

    Returns the parsed :class:`date` from the first matching cell, or ``None``
    if no footer row is present or the month name is unrecognized. Used as
    priority-2 of the E*TRADE ``as_of`` derivation ladder (REQ-FIX-WLT-003).
    """
    for row in rows:
        for cell in row:
            if not cell or "generated at" not in cell.lower():
                continue
            m = _GENERATED_AT_RE.search(cell)
            if m is None:
                continue
            month = _MONTH_NAMES.get(m.group(1).lower())
            if month is None:
                continue
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                continue
    return None

# Action mapping (case-sensitive — E*TRADE strings are stable)
_ACTION_MAP: dict[str, str] = {
    "Bought": CanonicalAction.BUY.value,
    "Sold": CanonicalAction.SELL.value,
    "Dividend": CanonicalAction.DIVIDEND_ORDINARY.value,
    "Qualified Dividend": CanonicalAction.DIVIDEND_QUALIFIED.value,
    "Dividend Reinvestment": CanonicalAction.REINVEST.value,
    "Interest": CanonicalAction.INTEREST.value,
    "Interest Income": CanonicalAction.INTEREST.value,
    "Stock Split": CanonicalAction.STOCK_SPLIT.value,
    "Transfer": CanonicalAction.TRANSFER.value,
    "Wire": CanonicalAction.TRANSFER.value,
    "Direct Debit": CanonicalAction.TRANSFER.value,
    "Online Transfer": CanonicalAction.TRANSFER.value,
    "Adjustment": CanonicalAction.OTHER.value,
    "Reorganization": CanonicalAction.OTHER.value,
}


# Account-line parser: last whitespace-separated token before "from" that
# starts with an optional minus and contains digits.
# E*TRADE stamps: "Account Activity for Cap 1(-6084) -6354 from 2024-05-03 to 2026-05-03"
# We want the trailing "-6354" → account_number "6354".
_ACCOUNT_LINE_RE = re.compile(
    r"Account Activity for .*?\s-?(\d{3,})\s+from\s",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _empty(s: str | None) -> bool:
    """E*TRADE uses '--' or empty string for missing values."""
    return s is None or s.strip() in ("", "--")


def _opt(s: str | None) -> str | None:
    """Return None for E*TRADE-empty values, else the stripped string."""
    if _empty(s):
        return None
    assert s is not None  # narrowed by _empty
    return s.strip()


def parse_account_line(line: str) -> str | None:
    """Extract the account number from line 3 of DownloadTxnHistory.csv.

    'Account Activity for Cap 1(-6084) -6354 from 2024-05-03 to 2026-05-03'
        → '6354'

    Returns None if no match.
    """
    if not line:
        return None
    m = _ACCOUNT_LINE_RE.search(line)
    if m:
        return m.group(1)
    return None


def map_action(activity_type: str) -> str:
    """Map an E*TRADE Activity Type to a CanonicalAction value (case-sensitive).

    Unknown actions fall through to 'other' — surface via raw_data for audit.
    """
    return _ACTION_MAP.get(activity_type.strip(), CanonicalAction.OTHER.value)


# --------------------------------------------------------------------------
# Parsed-row dataclass (intermediate stage, before DB write)
# --------------------------------------------------------------------------


@dataclass
class _ParsedTxn:
    """Intermediate parsed transaction (pre-DB)."""

    row_index: int
    trade_date: Any  # date
    settlement_date: Any  # date | None
    action: str
    canonical_action: str
    symbol: str | None
    cusip: str | None
    description: str | None
    quantity: Decimal | None
    price: Decimal | None
    amount: Decimal | None
    commission: Decimal | None
    raw_data: dict[str, Any]


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


class EtradeCsvAdapter(BaseAdapter):
    """Adapter for E*TRADE CSV exports.

    Constructor takes a folder path containing DownloadTxnHistory.csv and
    PortfolioDownload.csv. Inserts into Account / BrokerageTransaction /
    PositionSnapshot. Idempotent via (account_id, source_row_hash) UNIQUE.
    """

    def __init__(
        self, folder: Path | str, *, as_of_override: date | None = None
    ) -> None:
        self.folder = Path(folder)
        # REQ-FIX-WLT-003: optional CLI ``--as-of`` override (priority 1 of the
        # position-snapshot as_of derivation ladder).
        self.as_of_override = as_of_override

    @property
    def source(self) -> str:
        return Source.ETRADE_CSV.value

    # ----------------------------------------------------------------------
    # Public entry point
    # ----------------------------------------------------------------------

    def run(self, session: Session) -> AdapterResult:
        result = AdapterResult(source=self.source)

        # P1-008: explicitly log skip of tradesdownload.csv (sign-convention
        # is opposite and data duplicates DownloadTxnHistory — per PLAN.md TASK-08).
        skipped_path = self.folder / SKIPPED_FILENAME
        if skipped_path.exists():
            logger.info(
                "E*TRADE adapter: skipping %s (sign convention opposite; "
                "data duplicates DownloadTxnHistory — deferred to Phase 2).",
                SKIPPED_FILENAME,
            )

        try:
            account = self._process_transactions(session, result)
            if account is not None:
                self._process_positions(session, result, account)
        except Exception as exc:  # noqa: BLE001 — top-level safety net
            logger.exception("E*TRADE adapter failed")
            result.status = IngestionStatus.FAILURE
            result.errors.append(("etrade_adapter", str(exc)))

        self._write_ingestion_log(session, result)
        return result

    # ----------------------------------------------------------------------
    # Transactions
    # ----------------------------------------------------------------------

    def _process_transactions(
        self, session: Session, result: AdapterResult
    ) -> Account | None:
        """Process DownloadTxnHistory.csv. Returns the Account row or None."""
        path = self.folder / TRANSACTIONS_FILENAME
        if not path.exists():
            logger.info("E*TRADE: %s not found in %s", TRANSACTIONS_FILENAME, self.folder)
            return None

        # Read the entire file once so we can scan metadata then re-iterate
        # for the data rows.
        raw_rows = list(read_csv_tolerant(path))
        if not raw_rows:
            return None

        # Account discovery from line 3 (index 2). Tolerate variation by
        # scanning the first 6 rows for the account marker.
        account_number: str | None = None
        for row in raw_rows[:6]:
            joined = ",".join(row)
            account_number = parse_account_line(joined)
            if account_number:
                break

        if not account_number:
            logger.warning(
                "E*TRADE: could not parse account number from %s metadata", path.name
            )
            return None

        account = self._upsert_account(session, account_number)

        # Find the header row (skips the 6-row metadata block).
        header_index, header = find_header_row(iter(raw_rows), _TXN_REQUIRED_COLS)
        col = {name.strip(): i for i, name in enumerate(header)}

        # Parse data rows.
        data_rows = raw_rows[header_index + 1 :]
        parsed_rows: list[_ParsedTxn] = []
        for offset, row in enumerate(data_rows):
            if not row or all(_empty(c) for c in row):
                continue
            row_index = header_index + 1 + offset
            try:
                parsed = self._parse_txn_row(row, col, row_index)
            except Exception as exc:  # noqa: BLE001 — per-record isolation
                result.record_error(f"{path.name}:{row_index}", exc)
                continue
            if parsed is not None:
                parsed_rows.append(parsed)

        # Persist real rows first; collect those needing a synthesized partner.
        for parsed in parsed_rows:
            try:
                created = self._persist_txn(session, account, parsed, path.name)
                result.records_processed += 1
                if created is None:
                    result.records_skipped += 1
                    continue
                result.records_created += 1

                # Synthesize a paired dividend partner for single-row reinvests.
                if parsed.action == "Dividend Reinvestment":
                    syn_created = self._persist_synthetic_dividend_partner(
                        session, account, parsed, created, path.name
                    )
                    result.records_processed += 1
                    if syn_created is None:
                        result.records_skipped += 1
                    else:
                        result.records_created += 1
            except Exception as exc:  # noqa: BLE001 — per-record isolation
                result.record_error(f"{path.name}:{parsed.row_index}", exc)
                # P1-009: roll back any partial work so the next row starts clean.
                with contextlib.suppress(Exception):
                    session.rollback()
                continue

        session.commit()
        return account

    # ----------------------------------------------------------------------
    # Positions
    # ----------------------------------------------------------------------

    def _resolve_positions_as_of(
        self, rows: list[list[str]], path: Path
    ) -> tuple[datetime, str]:
        """Derive the positions snapshot ``as_of`` and its provenance label.

        Priority ladder (REQ-FIX-WLT-003):
            1. ``--as-of`` CLI override            → source "cli"
            2. embedded "Generated at ..." footer  → source "embedded"
            3. file mtime (UTC date)               → source "mtime"

        Returns a naive ``datetime`` (midnight of the derived date) so the
        value round-trips through the ``DateTime`` column, plus the provenance
        string recorded in ``raw_data["as_of_source"]``.
        """
        if self.as_of_override is not None:
            return datetime.combine(self.as_of_override, datetime.min.time()), "cli"

        embedded = parse_generated_at_date(rows)
        if embedded is not None:
            return datetime.combine(embedded, datetime.min.time()), "embedded"

        try:
            mtime_date = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()
        except OSError:
            mtime_date = datetime.now(UTC).date()
        return datetime.combine(mtime_date, datetime.min.time()), "mtime"

    def _process_positions(
        self, session: Session, result: AdapterResult, account: Account
    ) -> None:
        """Process PortfolioDownload.csv. Skips summary header rows."""
        path = self.folder / POSITIONS_FILENAME
        if not path.exists():
            logger.info("E*TRADE: %s not found in %s", POSITIONS_FILENAME, self.folder)
            return

        rows = list(read_csv_tolerant(path))
        if not rows:
            return

        try:
            header_index, header = find_header_row(iter(rows), _POS_REQUIRED_COLS)
        except ValueError:
            logger.warning("E*TRADE: could not locate position header in %s", path.name)
            return

        col = {name.strip(): i for i, name in enumerate(header)}
        data_rows = rows[header_index + 1 :]

        # As-of date derivation (REQ-FIX-WLT-003), highest priority first:
        #   1. CLI --as-of override (self.as_of_override)
        #   2. embedded "Generated at <Month> <day> <year>" footer row
        #   3. file mtime (UTC date) — NOT datetime.now, so a re-import of the
        #      same export is idempotent and a fresh export on a new day writes
        #      a new snapshot.
        # as_of is now date-quantized INTO the dedup hash, so intra-day
        # re-imports stay idempotent while a next-day export inserts fresh rows.
        as_of, as_of_source = self._resolve_positions_as_of(rows, path)

        def _cell(row: list[str], name: str) -> str | None:
            """Defensive column lookup — returns None if the row is shorter
            than expected (E*TRADE summary rows like 'CASH,,,,...' or 'TOTAL'
            sometimes have fewer cells than the header)."""
            idx = col.get(name)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        for offset, row in enumerate(data_rows):
            if not row or all(_empty(c) for c in row):
                continue
            symbol = _opt(_cell(row, "Symbol"))
            if not symbol:
                continue
            # Skip CASH and TOTAL summary rows that some E*TRADE exports include.
            if symbol.upper() in {"TOTAL"}:
                continue
            # TASK-11: stricter shape filter — anything that doesn't look like
            # a real ticker (e.g. "Generated at May 4 2026 02:47 PM ET") is
            # treated as a metadata/footer row and skipped.
            if not _TICKER_RE.match(symbol.upper()):
                continue
            row_index = header_index + 1 + offset
            try:
                quantity = parse_quantity(_cell(row, "Quantity"))
                price = parse_currency(_cell(row, "Last Price $"))
                market_value = parse_currency(_cell(row, "Value $"))
                avg_cost = parse_currency(_cell(row, "Price Paid $"))
                total_gain = parse_currency(_cell(row, "Total Gain $"))

                # REQ-FIX-WLT-003: derive cost_basis = avg_cost × quantity when
                # both are present (E*TRADE ships avg cost + qty but no total
                # basis); quantize to cents. Else leave None.
                cost_basis: Decimal | None = None
                if avg_cost is not None and quantity is not None:
                    cost_basis = (avg_cost * quantity).quantize(Decimal("0.01"))

                row_hash = compute_position_row_hash(
                    broker=Broker.ETRADE.value,
                    account_number=account.account_number,
                    source_file=path.name,
                    row_index=row_index,
                    # REQ-FIX-WLT-003: date-quantized as_of in the hash so a
                    # fresh export on a new date writes a fresh snapshot while
                    # intra-day re-imports stay idempotent.
                    as_of_iso=as_of.date().isoformat(),
                    symbol=symbol,
                    quantity=quantity,
                )

                # Idempotency: skip if already present.
                exists = (
                    session.query(PositionSnapshot)
                    .filter(
                        PositionSnapshot.account_id == account.id,
                        PositionSnapshot.source_row_hash == row_hash,
                    )
                    .first()
                )
                if exists is not None:
                    result.records_skipped += 1
                    result.records_processed += 1
                    continue

                pos = PositionSnapshot(
                    account_id=account.id,
                    as_of=as_of,
                    symbol=symbol,
                    description=None,
                    quantity=quantity,
                    price=price,
                    market_value=market_value,
                    cost_basis=cost_basis,
                    avg_cost_basis=avg_cost,
                    unrealized_gain=total_gain,
                    source_file=path.name,
                    source_row_hash=row_hash,
                    raw_data={
                        "row": row,
                        "header": header,
                        "as_of_source": as_of_source,
                    },
                )
                session.add(pos)
                result.records_created += 1
                result.records_processed += 1
            except Exception as exc:  # noqa: BLE001
                result.record_error(f"{path.name}:{row_index}", exc)
                continue

        session.commit()

    # ----------------------------------------------------------------------
    # Row parsing
    # ----------------------------------------------------------------------

    def _parse_txn_row(
        self, row: list[str], col: dict[str, int], row_index: int
    ) -> _ParsedTxn | None:
        def cell(name: str) -> str | None:
            idx = col.get(name)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        trade_raw = cell("Activity/Trade Date")
        trade_date = parse_date_flexible(trade_raw)
        if trade_date is None:
            return None

        settlement_date = parse_date_flexible(cell("Settlement Date"))
        action = (cell("Activity Type") or "").strip()

        symbol = _opt(cell("Symbol"))
        cusip = _opt(cell("Cusip"))
        description = _opt(cell("Description"))
        quantity = parse_quantity(cell("Quantity #") or "")
        price = parse_currency(cell("Price $") or "")
        amount = parse_currency(cell("Amount $") or "")
        commission = parse_currency(cell("Commission") or "")

        canonical = map_action(action)
        # P2-001 / REQ-005b: quantity must always be stored positive.
        if canonical == CanonicalAction.SELL.value and quantity is not None and quantity < 0:
            quantity = -quantity

        raw_data: dict[str, Any] = {
            "header": [k for k, _ in sorted(col.items(), key=lambda kv: kv[1])],
            "row": row,
        }

        return _ParsedTxn(
            row_index=row_index,
            trade_date=trade_date,
            settlement_date=settlement_date,
            action=action,
            canonical_action=canonical,
            symbol=symbol,
            cusip=cusip,
            description=description,
            quantity=quantity,
            price=price,
            amount=amount,
            commission=commission,
            raw_data=raw_data,
        )

    # ----------------------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------------------

    def _upsert_account(self, session: Session, account_number: str) -> Account:
        """Find or insert the Account row for this E*TRADE account."""
        existing = (
            session.query(Account)
            .filter(
                Account.broker == Broker.ETRADE.value,
                Account.account_number == account_number,
            )
            .first()
        )
        if existing is not None:
            return existing

        account = Account(
            broker=Broker.ETRADE.value,
            account_number=account_number,
            account_name=None,
            account_type=AccountType.TAXABLE.value,
            entity=Entity.PERSONAL.value,
            tax_sheltered=False,
            is_plan_wrapper=False,
        )
        session.add(account)
        session.flush()
        return account

    def _persist_txn(
        self,
        session: Session,
        account: Account,
        parsed: _ParsedTxn,
        source_file: str,
    ) -> BrokerageTransaction | None:
        """Insert a real transaction row. Returns None if duplicate (skipped)."""
        row_hash = compute_brokerage_row_hash(
            broker=Broker.ETRADE.value,
            account_number=account.account_number,
            source_file=source_file,
            row_index=parsed.row_index,
            trade_date=parsed.trade_date,
            action=parsed.action,
            symbol=parsed.symbol,
            quantity=parsed.quantity,
            amount=parsed.amount,
        )

        existing = (
            session.query(BrokerageTransaction)
            .filter(
                BrokerageTransaction.account_id == account.id,
                BrokerageTransaction.source_row_hash == row_hash,
            )
            .first()
        )
        if existing is not None:
            return None

        tx = BrokerageTransaction(
            account_id=account.id,
            trade_date=parsed.trade_date,
            settlement_date=parsed.settlement_date,
            action=parsed.action,
            canonical_action=parsed.canonical_action,
            symbol=parsed.symbol,
            cusip=parsed.cusip,
            description=parsed.description,
            quantity=parsed.quantity,
            price=parsed.price,
            amount=parsed.amount,
            commission=parsed.commission,
            fees=None,
            paired_transaction_id=None,
            is_synthetic=False,
            status=BrokerageTxStatus.IMPORTED.value,
            source_file=source_file,
            source_row_hash=row_hash,
            raw_data=parsed.raw_data,
        )
        session.add(tx)
        session.flush()
        return tx

    def _persist_synthetic_dividend_partner(
        self,
        session: Session,
        account: Account,
        parsed: _ParsedTxn,
        real_tx: BrokerageTransaction,
        source_file: str,
    ) -> BrokerageTransaction | None:
        """Synthesize the paired ordinary-dividend row for a Dividend Reinvestment.

        E*TRADE issues a single 'Dividend Reinvestment' row with the buy details
        (negative cash-out amount). We synthesize the cash-in dividend side so
        downstream tools see a normal dividend → reinvest pair.

        Idempotency: stable hash via synthetic_suffix='div_partner'.
        """
        synth_amount = abs(parsed.amount) if parsed.amount is not None else None
        synth_action = "Dividend (synthesized)"

        row_hash = compute_brokerage_row_hash(
            broker=Broker.ETRADE.value,
            account_number=account.account_number,
            source_file=source_file,
            row_index=parsed.row_index,
            trade_date=parsed.trade_date,
            action=parsed.action,
            symbol=parsed.symbol,
            quantity=parsed.quantity,
            amount=parsed.amount,
            synthetic_suffix="div_partner",
        )

        existing = (
            session.query(BrokerageTransaction)
            .filter(
                BrokerageTransaction.account_id == account.id,
                BrokerageTransaction.source_row_hash == row_hash,
            )
            .first()
        )
        if existing is not None:
            return None

        synth = BrokerageTransaction(
            account_id=account.id,
            trade_date=parsed.trade_date,
            settlement_date=parsed.settlement_date,
            action=synth_action,
            canonical_action=CanonicalAction.DIVIDEND_ORDINARY.value,
            symbol=parsed.symbol,
            cusip=parsed.cusip,
            description=parsed.description,
            quantity=None,
            price=None,
            amount=synth_amount,
            commission=None,
            fees=None,
            paired_transaction_id=real_tx.id,
            is_synthetic=True,
            status=BrokerageTxStatus.IMPORTED.value,
            source_file=source_file,
            source_row_hash=row_hash,
            raw_data={"synthesized_from": real_tx.id, "real_row": parsed.raw_data},
        )
        session.add(synth)
        session.flush()
        # P1-006: bidirectional FK — the real row also links to its synthetic partner.
        real_tx.paired_transaction_id = synth.id
        session.flush()
        return synth

    # ----------------------------------------------------------------------
    # Audit
    # ----------------------------------------------------------------------

    @staticmethod
    def _write_ingestion_log(session: Session, result: AdapterResult) -> None:
        error_detail: str | None = None
        if result.errors:
            # P2-012: truncate to first 50 errors.
            lines = [f"{rid}: {msg}" for rid, msg in result.errors[:50]]
            error_detail = "\n\n".join(lines)

        log = IngestionLog(
            source=result.source,
            run_at=result.run_at,
            status=result.status.value,
            records_processed=result.records_processed,
            records_failed=result.records_failed,
            error_detail=error_detail,
            # P2-003: retryable on both PARTIAL_FAILURE and FAILURE (not just partial).
            retryable=result.status in (IngestionStatus.PARTIAL_FAILURE, IngestionStatus.FAILURE),
        )
        session.add(log)
        session.commit()


__all__ = [
    "EtradeCsvAdapter",
    "map_action",
    "parse_account_line",
    "parse_generated_at_date",
]
