"""Fidelity CSV adapter — ingests Accounts_History and Portfolio_Positions files
into the isolated brokerage tables.

REQ-005a..g (see ``proposals/brokerage-ingest/PLAN.md`` TASK-06):
- Account discovery: every row's (Account Number, Account name) → upsert into
  ``account``. Hard-coded account_type per known Fidelity account number.
- Multi-file ingest: process every ``Accounts_History*.csv`` + every
  ``Portfolio_Positions_*.csv`` file in the configured folder.
- Skip leading blank rows; scan for the header row by matching required columns.
- Skip trailing disclaimer / "Date downloaded" footer rows.
- Account ``89766`` (MS 401k plan wrapper) emits a 15-column row variant — when
  ``account_number=='89766' and len(row)==15`` we shift columns from index 7
  onward by +1 to realign with the 14-column header.
- Sells with negative quantity: stored quantity is positive (``abs``); canonical
  action is ``sell``.
- ``REINVESTMENT`` + ``DIVIDEND RECEIVED`` rows are persisted as separate rows
  and linked via ``paired_transaction_id`` (dividend → reinvest, in source order
  per (account, date, symbol, |amount|)).
- Action mapping: case-insensitive prefix match on the ``Action`` column.
- Idempotent re-ingest via ``compute_brokerage_row_hash``.

Inherits :class:`BaseAdapter`; returns :class:`AdapterResult`; writes one
``IngestionLog`` row at end (see ``stripe_adapter.py:479-527`` for the canonical
commit pattern).
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

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


# ── Header signatures ─────────────────────────────────────────────────────
HISTORY_REQUIRED_COLS = {"Run Date", "Account", "Account Number", "Action"}
HISTORY_HEADER_LEN = 14  # canonical header column count

POSITIONS_REQUIRED_COLS = {"Account Number", "Symbol", "Quantity", "Last Price"}


# ── Hard-coded account_type per known Fidelity account number ─────────────
# (See PLAN TASK-11 / IDEATION; user can amend later.)
ACCOUNT_TYPE_BY_NUMBER: dict[str, str] = {
    "Z23257759": AccountType.TOD.value,
    "653373015": AccountType.BROKERAGELINK.value,
    "89766": AccountType.K401.value,
    "241527012": AccountType.HSA.value,
}

# Account 89766 is a plan wrapper (paper-only valuations, no P&L flow).
PLAN_WRAPPER_ACCOUNTS = {"89766"}


# ── Action prefix → canonical_action mapping ──────────────────────────────
# Order matters — first match wins. All matched case-insensitively.
_ACTION_PREFIX_MAP: list[tuple[str, str]] = [
    ("YOU BOUGHT", CanonicalAction.BUY.value),
    ("YOU SOLD", CanonicalAction.SELL.value),
    ("DIVIDEND RECEIVED", CanonicalAction.DIVIDEND_ORDINARY.value),
    ("REINVESTMENT", CanonicalAction.REINVEST.value),
    ("INTEREST", CanonicalAction.INTEREST.value),
    ("EXCHANGE IN", CanonicalAction.EXCHANGE.value),
    ("EXCHANGE OUT", CanonicalAction.EXCHANGE.value),
    ("TRANSFERRED FROM", CanonicalAction.TRANSFER.value),
    # P2-013: JOURNAL maps to JOURNAL (not TRANSFER) — consistent with Schwab.
    # 'TRANSFERRED FROM' covers actual transfers; JOURNAL entries are distinct events.
    ("JOURNAL", CanonicalAction.JOURNAL.value),
    ("ELECTRONIC FUNDS TRANSFER PAID", CanonicalAction.TRANSFER.value),
    ("ELECTRONIC FUNDS TRANSFER RECEIVED", CanonicalAction.TRANSFER.value),
    ("DEPOSIT", CanonicalAction.CONTRIBUTION.value),
    ("CONTRIBUTION", CanonicalAction.CONTRIBUTION.value),
    ("WITHDRAWAL", CanonicalAction.DISTRIBUTION.value),
    ("DISTRIBUTION", CanonicalAction.DISTRIBUTION.value),
    ("CHANGE ON MARKET VALUE", CanonicalAction.VALUATION_ADJUSTMENT.value),
]


def map_action(action_text: str) -> str:
    """Map a Fidelity Action string to a CanonicalAction value.

    Case-insensitive prefix match; falls through to ``other``.
    """
    upper = action_text.strip().upper()
    for prefix, canonical in _ACTION_PREFIX_MAP:
        if upper.startswith(prefix):
            return canonical
    return CanonicalAction.OTHER.value


# ── Trailing footer detection ─────────────────────────────────────────────
def _is_footer_row(row: list[str], header_len: int) -> bool:
    """Return True when the row is part of the trailing disclaimer / footer."""
    # Truncated row (less than the header column count) → footer
    if len(row) < header_len:
        return True
    if not row:
        return True
    first = row[0].strip()
    if first.startswith(('"The data', "The data")):
        return True
    return first.startswith("Date downloaded")


# ── 89766 column-shift edge case ──────────────────────────────────────────
def _normalize_89766_row(account_number: str, row: list[str]) -> list[str]:
    """Re-align a 15-column 89766 row to the 14-column header layout.

    The MS 401k plan wrapper exports rows where (versus the canonical header)
    Price is omitted, Quantity is shifted left to index 7, and one extra empty
    cell is appended between Accrued Interest and Amount. We compensate by
    inserting an empty Price cell at index 7 and dropping the extra empty at
    original index 11, yielding a 14-cell row that lines up with the header
    (Quantity at idx 8, Amount at idx 12, Settlement at idx 13).
    """
    if account_number == "89766" and len(row) == 15:
        return row[:7] + [""] + row[7:11] + row[12:14]
    return row


# ── Filename → as_of date for positions files ─────────────────────────────
_POSITIONS_DATE_RE = re.compile(
    r"Portfolio_Positions_([A-Za-z]+)-(\d{1,2})-(\d{4})", re.IGNORECASE
)
_MONTH_NAMES = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_positions_filename_date(filename: str) -> date | None:
    """Extract the as_of date from a Portfolio_Positions_<MMM>-<DD>-<YYYY>.csv name."""
    m = _POSITIONS_DATE_RE.search(filename)
    if not m:
        return None
    month_name = m.group(1)[:3].title()
    month = _MONTH_NAMES.get(month_name)
    if month is None:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(2)))
    except ValueError:
        return None


class FidelityCsvAdapter(BaseAdapter):
    """Ingest Fidelity ``Accounts_History*.csv`` + ``Portfolio_Positions_*.csv``."""

    def __init__(self, folder: str | Path) -> None:
        self._folder = Path(folder)

    @property
    def source(self) -> str:
        return Source.FIDELITY_CSV.value

    # ── Public entry-point ───────────────────────────────────────────────
    def run(self, session: Session) -> AdapterResult:
        result = AdapterResult(source=self.source)

        if not self._folder.exists() or not self._folder.is_dir():
            logger.warning("Fidelity folder not found: %s", self._folder)
            self._write_log(session, result, error_detail=None)
            return result

        history_files = sorted(self._folder.glob("Accounts_History*.csv"))
        positions_files = sorted(self._folder.glob("Portfolio_Positions_*.csv"))

        for path in history_files:
            try:
                self._ingest_history_file(path, session, result)
            except Exception as exc:  # noqa: BLE001
                result.record_error(f"file:{path.name}", exc)

        for path in positions_files:
            try:
                self._ingest_positions_file(path, session, result)
            except Exception as exc:  # noqa: BLE001
                result.record_error(f"file:{path.name}", exc)

        # Final commit for any pending rows.
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            result.record_error("commit", exc)

        # Demote PARTIAL → FAILURE only when nothing succeeded.
        if (
            result.records_created == 0
            and result.records_skipped == 0
            and result.records_failed > 0
        ):
            result.status = IngestionStatus.FAILURE

        error_detail: str | None = None
        if result.errors:
            # P2-012: truncate to first 50 errors (matches Vanguard + Schwab pattern).
            error_detail = "\n\n".join(
                f"[{rid}]\n{msg}" for rid, msg in result.errors[:50]
            )

        self._write_log(session, result, error_detail=error_detail)

        logger.info(
            "FidelityCsvAdapter run complete: status=%s created=%d skipped=%d failed=%d",
            result.status,
            result.records_created,
            result.records_skipped,
            result.records_failed,
        )
        return result

    # ── Account upsert ───────────────────────────────────────────────────
    def _upsert_account(
        self, session: Session, account_number: str, account_name: str
    ) -> Account:
        """Look up or create an Account row for this (broker, account_number)."""
        existing = (
            session.query(Account)
            .filter(
                Account.broker == Broker.FIDELITY.value,
                Account.account_number == account_number,
            )
            .one_or_none()
        )
        if existing is not None:
            # Backfill name if the existing row didn't have one.
            if account_name and not existing.account_name:
                existing.account_name = account_name
            return existing

        acct_type = ACCOUNT_TYPE_BY_NUMBER.get(account_number, AccountType.TAXABLE.value)
        is_wrapper = account_number in PLAN_WRAPPER_ACCOUNTS
        acct = Account(
            broker=Broker.FIDELITY.value,
            account_number=account_number,
            account_name=account_name or None,
            account_type=acct_type,
            entity=Entity.PERSONAL.value,
            tax_sheltered=acct_type
            in {
                AccountType.HSA.value,
                AccountType.K401.value,
                AccountType.K403B.value,
                AccountType.K529.value,
                AccountType.ROTH_IRA.value,
                AccountType.TRAD_IRA.value,
                AccountType.BROKERAGELINK.value,
            },
            is_plan_wrapper=is_wrapper,
        )
        session.add(acct)
        session.flush()
        return acct

    # ── History ingestion ────────────────────────────────────────────────
    def _ingest_history_file(
        self, path: Path, session: Session, result: AdapterResult
    ) -> None:
        all_rows: list[list[str]] = list(read_csv_tolerant(path))

        # Skip leading blank rows by finding the header position.
        header_idx, header_row = find_header_row(all_rows, HISTORY_REQUIRED_COLS)
        header_len = len(header_row)

        # Build (column_name → index) lookup from the canonical header.
        col: dict[str, int] = {name.strip(): i for i, name in enumerate(header_row)}

        # Walk data rows.
        # Track REINVESTMENT rows by (account, date, symbol, |amount|) so a
        # following DIVIDEND RECEIVED with the same key can pair to it.
        # The expected source order is REINVEST first, then DIVIDEND.
        # The PLAN says "dividend → reinvest" so the dividend row's
        # paired_transaction_id points to the matching reinvestment row.
        pending_reinvests: dict[tuple[str, str, str, str], str] = {}

        for row_idx_offset, raw_row in enumerate(
            all_rows[header_idx + 1 :], start=header_idx + 1
        ):
            # P3: blank rows within the data section are skipped (continue); only
            # actual disclaimer text or "Date downloaded" triggers an end-of-data
            # break to avoid discarding legitimate rows after an empty spacer.
            if not raw_row or all(not c.strip() for c in raw_row):
                continue  # blank spacer row — keep scanning
            if _is_footer_row(raw_row, header_len):
                # Disclaimer text or truncated row → end of data.
                break

            account_number = (raw_row[col["Account Number"]] or "").strip()
            account_name = (raw_row[col["Account"]] or "").strip()

            # Apply the 89766 15-column shift BEFORE re-parsing fields.
            shifted = _normalize_89766_row(account_number, raw_row)
            if len(shifted) < header_len:
                # After shift it should match; otherwise it's a malformed row.
                result.record_error(
                    f"{path.name}:row{row_idx_offset}",
                    ValueError(
                        f"row has {len(raw_row)} cols (shifted to {len(shifted)}), "
                        f"header={header_len}"
                    ),
                )
                continue

            try:
                self._process_history_row(
                    session=session,
                    result=result,
                    path=path,
                    row_idx=row_idx_offset,
                    raw_row=raw_row,
                    shifted=shifted,
                    col=col,
                    account_number=account_number,
                    account_name=account_name,
                    pending_reinvests=pending_reinvests,
                )
            except Exception as exc:  # noqa: BLE001
                result.record_error(f"{path.name}:row{row_idx_offset}", exc)
                # P1-005: roll back any partial work so the next row starts clean.
                with contextlib.suppress(Exception):
                    session.rollback()

    def _process_history_row(
        self,
        *,
        session: Session,
        result: AdapterResult,
        path: Path,
        row_idx: int,
        raw_row: list[str],
        shifted: list[str],
        col: dict[str, int],
        account_number: str,
        account_name: str,
        pending_reinvests: dict[tuple[str, str, str, str], str],
    ) -> None:
        result.records_processed += 1

        if not account_number:
            raise ValueError("Account Number is empty")

        account = self._upsert_account(session, account_number, account_name)

        # Pull and parse fields from the shifted row (header layout aligned).
        run_date_raw = shifted[col["Run Date"]]
        action_text = (shifted[col["Action"]] or "").strip()
        symbol_raw = (shifted[col["Symbol"]] or "").strip() or None
        description = (shifted[col["Description"]] or "").strip() or None

        price_raw = shifted[col["Price ($)"]] if "Price ($)" in col else ""
        quantity_raw = shifted[col["Quantity"]] if "Quantity" in col else ""
        commission_raw = (
            shifted[col["Commission ($)"]] if "Commission ($)" in col else ""
        )
        fees_raw = shifted[col["Fees ($)"]] if "Fees ($)" in col else ""
        amount_raw = shifted[col["Amount ($)"]] if "Amount ($)" in col else ""
        settlement_raw = (
            shifted[col["Settlement Date"]] if "Settlement Date" in col else ""
        )

        run_date = parse_date_flexible(run_date_raw)
        if run_date is None:
            raise ValueError(f"Cannot parse Run Date {run_date_raw!r}")
        settlement_date = parse_date_flexible(settlement_raw) if settlement_raw else None

        canonical = map_action(action_text)

        quantity = parse_quantity(quantity_raw)
        amount = parse_currency(amount_raw)
        price = parse_currency(price_raw)
        commission = parse_currency(commission_raw)
        fees = parse_currency(fees_raw)

        # SELL with negative quantity → store as positive.
        if canonical == CanonicalAction.SELL.value and quantity is not None and quantity < 0:
            quantity = -quantity

        # Hash for dedup. Includes source_file + row_index so within-file
        # duplicates with identical fields stay distinct.
        row_hash = compute_brokerage_row_hash(
            broker=Broker.FIDELITY.value,
            account_number=account_number,
            source_file=path.name,
            row_index=row_idx,
            trade_date=run_date,
            action=action_text,
            symbol=symbol_raw,
            quantity=quantity,
            amount=amount,
        )

        # Idempotency check: same (account_id, source_row_hash) → skip.
        existing_id = (
            session.query(BrokerageTransaction.id)
            .filter(
                BrokerageTransaction.account_id == account.id,
                BrokerageTransaction.source_row_hash == row_hash,
            )
            .scalar()
        )
        if existing_id is not None:
            result.records_skipped += 1
            return

        tx = BrokerageTransaction(
            account_id=account.id,
            trade_date=run_date,
            settlement_date=settlement_date,
            action=action_text,
            canonical_action=canonical,
            symbol=symbol_raw,
            description=description,
            quantity=quantity,
            price=price,
            amount=amount,
            commission=commission,
            fees=fees,
            is_synthetic=False,
            status=BrokerageTxStatus.IMPORTED.value,
            source_file=path.name,
            source_row_hash=row_hash,
            raw_data={"row": list(raw_row), "header_index": list(col.keys())},
        )
        session.add(tx)
        session.flush()
        result.records_created += 1

        # ── Pairing logic ────────────────────────────────────────────────
        # REINVESTMENT and DIVIDEND RECEIVED rows are linked. Source order is
        # REINVEST first, then DIVIDEND. The dividend's paired_transaction_id
        # points to the matching reinvestment row.
        #
        # P1-001: normalize amount to 2 decimal places before using as a key
        # so Decimal("100.50") and Decimal("100.5") produce the same string.
        amount_key = ""
        if amount is not None:
            amount_key = str(abs(amount).quantize(Decimal("0.01")))
        sym_key = symbol_raw or ""
        pair_key = (account.id, run_date.isoformat(), sym_key, amount_key)

        if canonical == CanonicalAction.REINVEST.value:
            pending_reinvests[pair_key] = tx.id
        elif canonical == CanonicalAction.DIVIDEND_ORDINARY.value:
            partner_id = pending_reinvests.pop(pair_key, None)
            if partner_id is not None:
                tx.paired_transaction_id = partner_id
                session.flush()

    # ── Positions ingestion ──────────────────────────────────────────────
    def _ingest_positions_file(
        self, path: Path, session: Session, result: AdapterResult
    ) -> None:
        all_rows: list[list[str]] = list(read_csv_tolerant(path))

        # Header lives near the top; skip any leading blank rows.
        header_idx, header_row = find_header_row(all_rows, POSITIONS_REQUIRED_COLS)
        header_len = len(header_row)
        col: dict[str, int] = {name.strip(): i for i, name in enumerate(header_row)}

        as_of_date = _parse_positions_filename_date(path.name) or date.today()
        as_of_dt = datetime.combine(as_of_date, datetime.min.time())
        as_of_iso = as_of_date.isoformat()

        for row_idx_offset, raw_row in enumerate(
            all_rows[header_idx + 1 :], start=header_idx + 1
        ):
            if _is_footer_row(raw_row, header_len):
                break
            try:
                self._process_position_row(
                    session=session,
                    result=result,
                    path=path,
                    row_idx=row_idx_offset,
                    raw_row=raw_row,
                    col=col,
                    as_of_dt=as_of_dt,
                    as_of_iso=as_of_iso,
                )
            except Exception as exc:  # noqa: BLE001
                result.record_error(f"{path.name}:row{row_idx_offset}", exc)

    def _process_position_row(
        self,
        *,
        session: Session,
        result: AdapterResult,
        path: Path,
        row_idx: int,
        raw_row: list[str],
        col: dict[str, int],
        as_of_dt: datetime,
        as_of_iso: str,
    ) -> None:
        result.records_processed += 1

        account_number = (raw_row[col["Account Number"]] or "").strip()
        account_name = (
            (raw_row[col["Account Name"]] or "").strip()
            if "Account Name" in col
            else ""
        )
        if not account_number:
            raise ValueError("Account Number is empty")

        account = self._upsert_account(session, account_number, account_name)

        symbol = (raw_row[col["Symbol"]] or "").strip() or None
        description = (
            (raw_row[col["Description"]] or "").strip() or None
            if "Description" in col
            else None
        )
        quantity = parse_quantity(raw_row[col["Quantity"]])
        price = parse_currency(raw_row[col["Last Price"]])
        market_value = (
            parse_currency(raw_row[col["Current Value"]])
            if "Current Value" in col
            else None
        )
        cost_basis = (
            parse_currency(raw_row[col["Cost Basis Total"]])
            if "Cost Basis Total" in col
            else None
        )
        avg_cost = (
            parse_currency(raw_row[col["Average Cost Basis"]])
            if "Average Cost Basis" in col
            else None
        )
        unrealized = (
            parse_currency(raw_row[col["Total Gain/Loss Dollar"]])
            if "Total Gain/Loss Dollar" in col
            else None
        )

        row_hash = compute_position_row_hash(
            broker=Broker.FIDELITY.value,
            account_number=account_number,
            source_file=path.name,
            row_index=row_idx,
            as_of_iso=as_of_iso,
            symbol=symbol,
            quantity=quantity,
        )

        existing_id = (
            session.query(PositionSnapshot.id)
            .filter(
                PositionSnapshot.account_id == account.id,
                PositionSnapshot.source_row_hash == row_hash,
            )
            .scalar()
        )
        if existing_id is not None:
            result.records_skipped += 1
            return

        snap = PositionSnapshot(
            account_id=account.id,
            as_of=as_of_dt,
            symbol=symbol,
            description=description,
            quantity=quantity,
            price=price,
            market_value=market_value,
            cost_basis=cost_basis,
            avg_cost_basis=avg_cost,
            unrealized_gain=unrealized,
            source_file=path.name,
            source_row_hash=row_hash,
            raw_data={"row": list(raw_row), "header_index": list(col.keys())},
        )
        session.add(snap)
        session.flush()
        result.records_created += 1

    # ── IngestionLog writer ──────────────────────────────────────────────
    def _write_log(
        self,
        session: Session,
        result: AdapterResult,
        *,
        error_detail: str | None,
    ) -> None:
        log = IngestionLog(
            source=self.source,
            run_at=result.run_at,
            status=result.status.value,
            records_processed=result.records_processed,
            records_failed=result.records_failed,
            error_detail=error_detail,
            retryable=result.status
            in (IngestionStatus.PARTIAL_FAILURE, IngestionStatus.FAILURE),
        )
        session.add(log)
        try:
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("Failed to write IngestionLog for FidelityCsvAdapter")


__all__ = [
    "FidelityCsvAdapter",
    "map_action",
]
