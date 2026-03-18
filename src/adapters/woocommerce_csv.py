"""WooCommerce order export CSV adapter.

Ingests a WooCommerce order export CSV into the BlackLine MTB transaction register.

REQ-ID: WOOCOMMERCE-CSV-001  Parse WooCommerce order export CSV, mapping standard
                              columns to Transaction schema.
REQ-ID: WOOCOMMERCE-CSV-002  Per-record error isolation — one bad row never halts
                              the batch (inherited from BaseAdapter contract).
REQ-ID: WOOCOMMERCE-CSV-003  Deduplicates via SHA256(source, order_number).
REQ-ID: WOOCOMMERCE-CSV-004  Maps to entity=blackline, tax_category=SALES_INCOME,
                              direction=income by default.
REQ-ID: WOOCOMMERCE-CSV-005  Only imports orders with status in IMPORTABLE_STATUSES
                              (completed, processing); pending/cancelled/refunded are
                              skipped.

Design spec: docs/superpowers/specs/2026-03-15-accounting-system-design.md §Adapters
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from src.adapters.base import AdapterResult, BaseAdapter
from src.models.enums import Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash

logger = logging.getLogger(__name__)

# Order statuses we accept as real revenue events.
IMPORTABLE_STATUSES = frozenset({"completed", "processing", "wc-completed", "wc-processing"})

# ---------------------------------------------------------------------------
# Column name constants — WooCommerce uses these headers in their default
# export.  We normalise header names (strip, lower) before matching so
# minor casing variations are tolerated.
# ---------------------------------------------------------------------------

# Possible header aliases for each logical field (normalised to lower-case).
_COL_ORDER_NUMBER = {"order number", "order id", "order_number", "order_id", "id", "#"}
_COL_ORDER_DATE = {"order date", "date", "order_date"}
_COL_STATUS = {"order status", "status", "order_status"}
_COL_TOTAL = {"order total", "total", "order_total", "amount"}
_COL_PAYMENT_METHOD = {"payment method", "payment_method", "payment method title", "payment_method_title"}
_COL_CUSTOMER_NAME = {
    "customer name", "customer_name",
    "billing first name + billing last name",
}
_COL_FIRST_NAME = {"billing first name", "billing_first_name"}
_COL_LAST_NAME = {"billing last name", "billing_last_name"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_header(headers: list[str], aliases: set[str]) -> str | None:
    """Return the first header (from *headers*) that matches any alias (case-insensitive)."""
    norm = {h.lower().strip(): h for h in headers}
    for alias in aliases:
        if alias in norm:
            return norm[alias]
    return None


def _parse_decimal(raw: str) -> Decimal | None:
    """Parse a WooCommerce amount string to Decimal.

    Handles currency symbols, comma thousands, and blank values.
    Returns None for blank strings; raises ValueError for un-parseable non-blank.
    """
    raw = raw.strip()
    if not raw:
        return None
    # Strip currency symbols, spaces, commas
    raw = re.sub(r"[$£€¥,\s]", "", raw)
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse amount {raw!r}: {exc}") from exc


def _parse_date(raw: str) -> str:
    """Parse a WooCommerce date string to ISO YYYY-MM-DD.

    Tries several common formats: YYYY-MM-DD, MM/DD/YYYY, DD/MM/YYYY, M/D/YY.
    Raises ValueError if none match.
    """
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date {raw!r} — unrecognised format")


# ---------------------------------------------------------------------------
# Row-level data structures
# ---------------------------------------------------------------------------

@dataclass
class WooRow:
    """One successfully parsed WooCommerce order row."""

    row_number: int   # 1-based
    order_number: str
    date: str         # ISO YYYY-MM-DD
    status: str       # normalised lower-case
    total: Decimal
    payment_method: str | None
    customer_name: str | None
    raw: dict[str, str]


@dataclass
class WooRowError:
    """A row that could not be parsed."""

    row_number: int
    reason: str
    raw: dict[str, str]


@dataclass
class WooParseResult:
    """Output of :func:`parse_woocommerce_csv`."""

    rows: list[WooRow] = field(default_factory=list)
    errors: list[WooRowError] = field(default_factory=list)
    skipped_statuses: list[tuple[int, str]] = field(default_factory=list)  # (row_number, status)
    headers: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_woocommerce_csv(raw_bytes: bytes) -> WooParseResult:
    """Decode and parse *raw_bytes* as a WooCommerce order export CSV.

    - Detects and strips UTF-8 BOM.
    - Falls back through common encodings.
    - Skips rows whose order status is not in IMPORTABLE_STATUSES.
    - Per-record errors are isolated.

    Args:
        raw_bytes: Raw bytes of the uploaded CSV file.

    Returns:
        :class:`WooParseResult` with parsed rows, per-record errors, and
        skipped-status entries.
    """
    # Encoding detection — strip BOM if present
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw_bytes.decode("latin-1", errors="replace")

    result = WooParseResult()
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        return result  # empty file

    headers = [h.strip() for h in reader.fieldnames if h]
    result.headers = headers

    # Resolve column names once against the actual headers
    col_order_number = _find_header(headers, _COL_ORDER_NUMBER)
    col_date = _find_header(headers, _COL_ORDER_DATE)
    col_status = _find_header(headers, _COL_STATUS)
    col_total = _find_header(headers, _COL_TOTAL)
    col_payment = _find_header(headers, _COL_PAYMENT_METHOD)
    col_customer = _find_header(headers, _COL_CUSTOMER_NAME)
    col_first = _find_header(headers, _COL_FIRST_NAME)
    col_last = _find_header(headers, _COL_LAST_NAME)

    if not col_order_number:
        raise ValueError(
            f"WooCommerce CSV is missing an order number column. "
            f"Expected one of: {sorted(_COL_ORDER_NUMBER)}. "
            f"Found headers: {headers}"
        )
    if not col_date:
        raise ValueError(
            f"WooCommerce CSV is missing a date column. "
            f"Expected one of: {sorted(_COL_ORDER_DATE)}. "
            f"Found headers: {headers}"
        )
    if not col_total:
        raise ValueError(
            f"WooCommerce CSV is missing a total column. "
            f"Expected one of: {sorted(_COL_TOTAL)}. "
            f"Found headers: {headers}"
        )

    for row_number, raw_row in enumerate(reader, start=1):
        norm: dict[str, str] = {k.strip(): v.strip() for k, v in raw_row.items() if k}
        try:
            # ── Order number ──────────────────────────────────────────────
            order_number = norm.get(col_order_number, "").strip()
            if not order_number:
                raise ValueError(f"Order number column {col_order_number!r} is empty")

            # ── Date ──────────────────────────────────────────────────────
            raw_date = norm.get(col_date, "").strip()
            if not raw_date:
                raise ValueError(f"Date column {col_date!r} is empty")
            date_str = _parse_date(raw_date)

            # ── Status ────────────────────────────────────────────────────
            raw_status = ""
            if col_status:
                raw_status = norm.get(col_status, "").strip()
            status = raw_status.lower().replace(" ", "-")

            if status and status not in IMPORTABLE_STATUSES:
                result.skipped_statuses.append((row_number, raw_status))
                continue

            # ── Total ─────────────────────────────────────────────────────
            raw_total = norm.get(col_total, "").strip()
            total = _parse_decimal(raw_total)
            if total is None:
                raise ValueError(f"Total column {col_total!r} is empty")
            if total <= Decimal("0"):
                raise ValueError(
                    f"Order {order_number} has non-positive total {total}; "
                    "refunds/credits should be handled separately"
                )

            # ── Payment method (optional) ──────────────────────────────────
            payment_method: str | None = None
            if col_payment:
                payment_method = norm.get(col_payment, "").strip() or None

            # ── Customer name (optional) ───────────────────────────────────
            customer_name: str | None = None
            if col_customer:
                customer_name = norm.get(col_customer, "").strip() or None
            elif col_first or col_last:
                first = (norm.get(col_first, "") if col_first else "").strip()
                last = (norm.get(col_last, "") if col_last else "").strip()
                combined = f"{first} {last}".strip()
                customer_name = combined or None

            result.rows.append(
                WooRow(
                    row_number=row_number,
                    order_number=order_number,
                    date=date_str,
                    status=status,
                    total=total,
                    payment_method=payment_method,
                    customer_name=customer_name,
                    raw=norm,
                )
            )

        except Exception as exc:
            result.errors.append(
                WooRowError(row_number=row_number, reason=str(exc), raw=norm)
            )

    return result


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class WooCommerceCsvAdapter(BaseAdapter):
    """Ingests a WooCommerce order export CSV into the BlackLine MTB register.

    Maps each completed/processing order to a Transaction with:
      - entity:       blackline
      - direction:    income
      - tax_category: SALES_INCOME
      - status:       needs_review

    Args:
        csv_bytes: Raw bytes of the uploaded CSV file.
        filename:  Original filename (used for source_id and raw_data).
        dry_run:   When True, parse but do not write to the database.
    """

    def __init__(
        self,
        csv_bytes: bytes,
        filename: str = "woocommerce_orders.csv",
        *,
        dry_run: bool = True,
    ) -> None:
        self._csv_bytes = csv_bytes
        self._filename = filename
        self._dry_run = dry_run

    @property
    def source(self) -> str:
        return Source.WOOCOMMERCE_CSV.value

    def run(self, session: Session) -> AdapterResult:
        """Parse the CSV and (unless dry_run) insert new Transaction rows.

        Returns:
            :class:`AdapterResult` with counts and per-record errors.
        """
        result = AdapterResult(source=self.source)

        try:
            parse_result = parse_woocommerce_csv(self._csv_bytes)
        except ValueError as exc:
            # Header-level failure — entire file unparseable
            result.record_error(self._filename, exc)
            return result

        # Collect per-row parse errors
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

        skipped = len(parse_result.skipped_statuses)
        if skipped:
            logger.info(
                "WooCommerceCsvAdapter: skipped %d rows with non-importable statuses in %s",
                skipped,
                self._filename,
            )

        return result

    def _process_row(
        self,
        row: WooRow,
        session: Session,
        result: AdapterResult,
    ) -> None:
        """Insert one WooCommerce order as a Transaction (skipped in dry_run)."""
        source_id = f"{self._filename}:order:{row.order_number}"
        source_hash = compute_source_hash(self.source, source_id)

        # Dedup check
        existing = (
            session.query(Transaction)
            .filter(Transaction.source_hash == source_hash)
            .first()
        )
        if existing is not None:
            result.records_skipped += 1
            return

        description_parts = [f"WooCommerce order #{row.order_number}"]
        if row.customer_name:
            description_parts.append(f"({row.customer_name})")
        description = " ".join(description_parts)

        tx = Transaction(
            source=self.source,
            source_id=source_id,
            source_hash=source_hash,
            date=row.date,
            description=description,
            amount=row.total,
            currency="USD",
            entity=Entity.BLACKLINE.value,
            direction=Direction.INCOME.value,
            tax_category=TaxCategory.SALES_INCOME.value,
            status=TransactionStatus.NEEDS_REVIEW.value,
            confidence=0.85,
            payment_method=row.payment_method,
            raw_data={
                "filename": self._filename,
                "order_number": row.order_number,
                "order_status": row.status,
                "row": row.raw,
            },
        )

        if not self._dry_run:
            session.add(tx)
            session.commit()
            result.records_created += 1
            logger.info(
                "WooCommerceCsvAdapter ingested order #%s: %s %s",
                row.order_number,
                row.date,
                row.total,
            )
        else:
            result.records_skipped += 1
