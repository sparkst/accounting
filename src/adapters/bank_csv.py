"""Bank CSV adapter — ingests bank statement CSV files into the register.

REQ-ID: BANK-CSV-001  Configurable column mapping per bank format, persisted in
                       a JSON sidecar file so repeat uploads reuse the same config.
REQ-ID: BANK-CSV-002  Encoding detection via chardet (handles UTF-8, Latin-1,
                       Windows-1252, and BOMs).
REQ-ID: BANK-CSV-003  Amount parsing handles parenthetical negatives, comma
                       thousands separators, and currency symbols.
REQ-ID: BANK-CSV-004  Date format validated against configured mapping; unparseable
                       dates are rejected per-record (per-record error isolation).
REQ-ID: BANK-CSV-005  Cross-references against existing transactions by
                       amount + date + payment_method to flag potential duplicates.
REQ-ID: BANK-CSV-006  Upload returns a preview (first 5 rows) before committing;
                       user confirms separately to persist.

Design spec: docs/superpowers/specs/2026-03-15-accounting-system-design.md §Bank CSV Adapter
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import chardet
from sqlalchemy.orm import Session

from src.adapters.base import AdapterResult, BaseAdapter
from src.models.enums import Source, TransactionStatus
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column mapping config
# ---------------------------------------------------------------------------

# Default mapping key → CSV column name.  Users override this per bank.
DEFAULT_COLUMN_MAP: dict[str, str] = {
    "date": "Date",
    "description": "Description",
    "amount": "Amount",
    # Optional columns — absent means None
    "debit": "",
    "credit": "",
    "balance": "",
    "check_number": "",
}

# Config directory for persisting per-bank mapping configs.
_CONFIG_DIR = Path("data/bank_csv_configs")


@dataclass
class BankCsvConfig:
    """Column mapping configuration for a named bank format.

    Attributes:
        bank_name:    Human-readable identifier (e.g. "chase_checking").
        date_column:  CSV header for the transaction date.
        date_format:  strptime format string (e.g. "%m/%d/%Y").
        description_column: CSV header for payee/description.
        amount_column: CSV header for a single signed amount column.
                       Mutually exclusive with debit_column/credit_column.
        debit_column:  CSV header for debit amounts (positive = money out).
        credit_column: CSV header for credit amounts (positive = money in).
        balance_column: Optional CSV header for running balance.
        entity:        Default entity for all rows from this bank (may be None).
        payment_method: Default payment_method label (e.g. "Chase ****1234").
    """

    bank_name: str
    date_column: str = "Date"
    date_format: str = "%m/%d/%Y"
    description_column: str = "Description"
    # Signed single-column amount (positive = credit/income, negative = debit/expense)
    amount_column: str = "Amount"
    # Separate debit / credit columns (alternative to amount_column)
    debit_column: str = ""
    credit_column: str = ""
    balance_column: str = ""
    entity: str | None = None
    payment_method: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bank_name": self.bank_name,
            "date_column": self.date_column,
            "date_format": self.date_format,
            "description_column": self.description_column,
            "amount_column": self.amount_column,
            "debit_column": self.debit_column,
            "credit_column": self.credit_column,
            "balance_column": self.balance_column,
            "entity": self.entity,
            "payment_method": self.payment_method,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BankCsvConfig:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    # ── Persistence ──────────────────────────────────────────────────────────

    @classmethod
    def config_path(cls, bank_name: str) -> Path:
        return _CONFIG_DIR / f"{bank_name}.json"

    def save(self) -> None:
        """Persist this config to data/bank_csv_configs/<bank_name>.json."""
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = self.config_path(self.bank_name)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        logger.debug("Saved bank CSV config: %s", path)

    @classmethod
    def load(cls, bank_name: str) -> BankCsvConfig | None:
        """Load a saved config, or return None if not found."""
        path = cls.config_path(bank_name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return cls.from_dict(data)
        except Exception as exc:
            logger.warning("Failed to load bank CSV config %s: %s", path, exc)
            return None

    @classmethod
    def list_saved(cls) -> list[str]:
        """Return a list of saved bank_name identifiers."""
        if not _CONFIG_DIR.exists():
            return []
        return [p.stem for p in sorted(_CONFIG_DIR.glob("*.json"))]


# ---------------------------------------------------------------------------
# Amount parsing
# ---------------------------------------------------------------------------

_PARENTHETICAL_RE = re.compile(r"^\s*\(([^)]+)\)\s*$")


def parse_amount(raw: str) -> Decimal | None:
    """Parse a bank CSV amount string into a Decimal.

    Handles:
    - Currency symbols: ``$1,234.56`` → ``1234.56``
    - Comma thousands: ``1,234.56`` → ``1234.56``
    - Parenthetical negatives: ``(1,234.56)`` → ``-1234.56``
    - Plain negatives: ``-1234.56``
    - Empty / whitespace → ``None``

    Returns:
        Decimal value, or ``None`` when the field is blank.

    Raises:
        ValueError: When the string is non-blank but cannot be parsed.
    """
    raw = raw.strip()
    if not raw:
        return None

    negative = False

    # Parenthetical negative: (1,234.56)
    m = _PARENTHETICAL_RE.match(raw)
    if m:
        raw = m.group(1)
        negative = True

    # Strip currency symbols and whitespace
    raw = re.sub(r"[$£€¥\s]", "", raw)
    # Strip comma thousands separators
    raw = raw.replace(",", "")

    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse amount {raw!r}: {exc}") from exc

    if negative:
        value = -abs(value)

    return value


# ---------------------------------------------------------------------------
# Encoding detection
# ---------------------------------------------------------------------------

def detect_encoding(raw_bytes: bytes) -> str:
    """Detect the character encoding of *raw_bytes*.

    Strips UTF-8/UTF-16 BOMs before detection.  Falls back to ``"utf-8"``
    when chardet confidence is below 0.5.

    Returns:
        Encoding name suitable for ``bytes.decode()``.
    """
    # Check for BOM explicitly — chardet sometimes misidentifies BOM files.
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"

    result = chardet.detect(raw_bytes)
    encoding: str = result.get("encoding") or "utf-8"
    confidence: float = result.get("confidence") or 0.0

    if confidence < 0.5:
        logger.debug(
            "chardet low confidence (%.2f) for encoding %r — falling back to utf-8",
            confidence,
            encoding,
        )
        return "utf-8"

    return encoding


# ---------------------------------------------------------------------------
# CSV parsing helpers
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    """One successfully parsed row from a bank CSV."""

    row_number: int  # 1-based row index within the CSV data rows
    date: str        # ISO YYYY-MM-DD
    description: str
    amount: Decimal  # positive = income, negative = expense
    raw: dict[str, str]  # verbatim CSV row


@dataclass
class RowError:
    """A row that could not be parsed."""

    row_number: int
    reason: str
    raw: dict[str, str]


@dataclass
class ParseResult:
    """Outcome of parsing a bank CSV file."""

    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    preview: list[ParsedRow] = field(default_factory=list)  # first 5 rows
    headers: list[str] = field(default_factory=list)


def parse_csv_bytes(
    raw_bytes: bytes,
    config: BankCsvConfig,
) -> ParseResult:
    """Decode and parse *raw_bytes* using *config* column mapping.

    - Encoding is auto-detected via chardet.
    - Column headers are whitespace-stripped before matching.
    - Date parsing uses ``config.date_format``; bad dates are per-record errors.
    - Amount is read from ``config.amount_column`` (single signed column) or
      from ``config.debit_column`` / ``config.credit_column`` (split columns).

    Returns:
        :class:`ParseResult` with rows, errors, preview, and headers.
    """
    encoding = detect_encoding(raw_bytes)
    try:
        text = raw_bytes.decode(encoding)
    except UnicodeDecodeError:
        # Last-resort fallback
        text = raw_bytes.decode("latin-1")

    result = ParseResult()
    # Strip leading BOM character if present (utf-8-sig decoding may leave it
    # in the first field name when the CSV reader does not handle it)
    text = text.lstrip("\ufeff")

    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        return result  # empty file

    # Strip whitespace from headers
    fieldnames = [h.strip() for h in reader.fieldnames]
    result.headers = fieldnames

    # Build a normalised header → original-header map for lookup
    header_map: dict[str, str] = {h.strip(): h for h in reader.fieldnames}

    def _get(row: dict[str, str], col: str) -> str:
        """Fetch *col* from *row*, stripping whitespace.  Returns "" if absent."""
        if not col:
            return ""
        orig = header_map.get(col, col)
        return row.get(orig, row.get(col, "")).strip()

    for row_number, raw_row in enumerate(reader, start=1):
        # Normalise keys in raw_row
        norm_row: dict[str, str] = {k.strip(): v for k, v in raw_row.items() if k}
        try:
            # ── Date ──────────────────────────────────────────────────────
            raw_date = _get(norm_row, config.date_column)
            if not raw_date:
                raise ValueError(f"Date column {config.date_column!r} is empty")
            try:
                dt = datetime.strptime(raw_date, config.date_format)
                date_str = dt.strftime("%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(
                    f"Cannot parse date {raw_date!r} with format {config.date_format!r}: {exc}"
                ) from exc

            # ── Description ───────────────────────────────────────────────
            description = _get(norm_row, config.description_column) or "(no description)"

            # ── Amount ────────────────────────────────────────────────────
            if config.debit_column or config.credit_column:
                # Separate debit/credit columns.
                # Debits = money out → negative amount.
                # Credits = money in → positive amount.
                debit_raw = _get(norm_row, config.debit_column)
                credit_raw = _get(norm_row, config.credit_column)
                debit_val = parse_amount(debit_raw) if debit_raw else None
                credit_val = parse_amount(credit_raw) if credit_raw else None

                if debit_val is not None and debit_val != Decimal("0"):
                    amount = -abs(debit_val)
                elif credit_val is not None and credit_val != Decimal("0"):
                    amount = abs(credit_val)
                else:
                    raise ValueError(
                        f"Both debit ({config.debit_column!r}) and "
                        f"credit ({config.credit_column!r}) are empty or zero"
                    )
            else:
                # Single signed amount column.
                raw_amount = _get(norm_row, config.amount_column)
                parsed = parse_amount(raw_amount)
                if parsed is None:
                    raise ValueError(
                        f"Amount column {config.amount_column!r} is empty"
                    )
                amount = parsed

            parsed_row = ParsedRow(
                row_number=row_number,
                date=date_str,
                description=description,
                amount=amount,
                raw=norm_row,
            )
            result.rows.append(parsed_row)
            if len(result.preview) < 5:
                result.preview.append(parsed_row)

        except Exception as exc:
            result.errors.append(
                RowError(row_number=row_number, reason=str(exc), raw=norm_row)
            )

    return result


# ---------------------------------------------------------------------------
# Dedup cross-reference
# ---------------------------------------------------------------------------

def find_cross_reference_matches(
    session: Session,
    date: str,
    amount: Decimal,
    payment_method: str | None,
) -> list[str]:
    """Return IDs of existing transactions that match on date + amount + payment_method.

    Used to flag potential reconciliation pairs (e.g. a Stripe payout matching
    a bank deposit).

    Returns:
        List of matching Transaction UUIDs (may be empty).
    """
    query = session.query(Transaction).filter(
        Transaction.date == date,
        Transaction.amount == amount,
    )
    if payment_method:
        query = query.filter(Transaction.payment_method == payment_method)
    return [tx.id for tx in query.all()]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class BankCsvAdapter(BaseAdapter):
    """Ingests a bank statement CSV file.

    Unlike other adapters, this one does not scan a directory — it operates on
    a single file provided at construction time.  The API route wraps the
    two-step preview → confirm flow.

    Args:
        csv_bytes:      Raw bytes of the uploaded CSV file.
        config:         Column mapping config for this bank format.
        filename:       Original filename (used as source_id base).
        dry_run:        When True, parse and cross-reference but do not commit
                        any rows to the database (preview mode).
    """

    def __init__(
        self,
        csv_bytes: bytes,
        config: BankCsvConfig,
        filename: str = "bank_statement.csv",
        *,
        dry_run: bool = True,
    ) -> None:
        self._csv_bytes = csv_bytes
        self._config = config
        self._filename = filename
        self._dry_run = dry_run

    @property
    def source(self) -> str:
        return Source.BANK_CSV.value

    def run(self, session: Session) -> AdapterResult:
        """Parse the CSV and (unless dry_run) insert new Transaction rows.

        In dry_run mode every row is parsed and cross-referenced, but nothing
        is written to the database.  Returns the normal AdapterResult; callers
        can inspect ``result.errors`` for per-record failures.
        """
        result = AdapterResult(source=self.source)
        parse_result = parse_csv_bytes(self._csv_bytes, self._config)

        # Collect per-row parse errors first.
        for row_err in parse_result.errors:
            result.record_error(
                f"{self._filename}:row{row_err.row_number}",
                ValueError(row_err.reason),
            )

        for row in parse_result.rows:
            result.records_processed += 1
            try:
                self._process_row(row, session, result)
            except Exception as exc:
                result.record_error(f"{self._filename}:row{row.row_number}", exc)

        return result

    def _process_row(
        self,
        row: ParsedRow,
        session: Session,
        result: AdapterResult,
    ) -> None:
        """Insert a single row as a Transaction (skipped in dry_run mode)."""
        # Build a stable source_id from filename + row number + date + amount.
        source_id = f"{self._filename}:{row.row_number}:{row.date}:{row.amount}"
        source_hash = compute_source_hash(self.source, source_id)

        # Check for existing transaction with same source_hash.
        existing = (
            session.query(Transaction)
            .filter(Transaction.source_hash == source_hash)
            .first()
        )
        if existing is not None:
            result.records_skipped += 1
            return

        # Cross-reference with existing transactions.
        payment_method = self._config.payment_method
        xref_ids = find_cross_reference_matches(
            session, row.date, row.amount, payment_method
        )
        review_reason: str | None = None
        if xref_ids:
            review_reason = (
                f"Possible duplicate/reconciliation match with existing "
                f"transaction(s): {', '.join(xref_ids[:3])}"
            )

        status = (
            TransactionStatus.NEEDS_REVIEW.value
            if (review_reason or row.amount is None)
            else TransactionStatus.NEEDS_REVIEW.value  # always needs review for bank imports
        )

        tx = Transaction(
            source=self.source,
            source_id=source_id,
            source_hash=source_hash,
            date=row.date,
            description=row.description,
            amount=row.amount,
            currency="USD",
            entity=self._config.entity,
            status=status,
            confidence=0.0,
            review_reason=review_reason,
            payment_method=payment_method,
            raw_data={"filename": self._filename, "row": row.raw},
        )

        if not self._dry_run:
            session.add(tx)
            session.commit()
            result.records_created += 1
            logger.info(
                "BankCsvAdapter ingested row %d: %s %s %s",
                row.row_number,
                row.date,
                row.description[:40],
                row.amount,
            )
        else:
            # Dry run — count as processed but not created.
            result.records_skipped += 1
