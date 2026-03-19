"""Brokerage CSV adapter — imports E*Trade, Schwab, and Vanguard 1099-B CSV data.

REQ-ID: ADAPTER-BROK-001  Format-specific parsers for E*Trade, Schwab, Vanguard CSV formats.
REQ-ID: ADAPTER-BROK-002  Tracks cost basis, short/long term classification in raw_data JSON.
REQ-ID: ADAPTER-BROK-003  Imports wash sale adjustment data from 1099-B CSV data.
REQ-ID: ADAPTER-BROK-004  Maps to INVESTMENT_INCOME / CAPITAL_GAIN_SHORT / CAPITAL_GAIN_LONG.
REQ-ID: ADAPTER-BROK-005  Each brokerage format has configurable column mappings.
REQ-ID: ADAPTER-BROK-006  Per-record error isolation (one bad row does not stop the batch).
REQ-ID: ADAPTER-BROK-007  Dedup by source_hash derived from (source, brokerage, row_key).

Design spec: docs/superpowers/specs/2026-03-15-accounting-system-design.md §Brokerage Adapter
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from src.adapters.base import AdapterResult, BaseAdapter
from src.models.enums import (
    Direction,
    Entity,
    Source,
    TaxCategory,
    TaxSubcategory,
    TransactionStatus,
)
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brokerage format identifiers
# ---------------------------------------------------------------------------

ETRADE = "etrade"
SCHWAB = "schwab"
VANGUARD = "vanguard"

SUPPORTED_BROKERAGES = (ETRADE, SCHWAB, VANGUARD)

# ---------------------------------------------------------------------------
# Column mapping definitions
# ---------------------------------------------------------------------------
# Each mapping is a dict of canonical_field -> list of possible CSV column headers
# (tried in order, first match wins, case-insensitive).

ETRADE_COLUMNS: dict[str, list[str]] = {
    "date_acquired": ["Date Acquired", "Acquisition Date"],
    "date_sold": ["Date Sold", "Date of Sale", "Sold Date"],
    "description": ["Security Description", "Description", "Security"],
    "quantity": ["Quantity", "Shares Sold", "Shares"],
    "proceeds": ["Proceeds", "Sales Proceeds", "Amount"],
    "cost_basis": ["Cost or Other Basis", "Adjusted Cost Basis", "Cost Basis"],
    "wash_sale_loss": ["Wash Sale Loss Disallowed", "Wash Sale Adj"],
    "gain_loss": ["Gain or Loss", "Net Gain/Loss", "Gain/Loss"],
    "term": ["Term", "Short/Long"],  # "Short" or "Long"
    "covered": ["Covered/Uncovered", "Box"],
}

SCHWAB_COLUMNS: dict[str, list[str]] = {
    "date_acquired": ["Date Acquired", "Acquisition Date", "Open Date"],
    "date_sold": ["Date Sold", "Close Date"],
    "description": ["Security Description", "Description", "Symbol / Description"],
    "quantity": ["Quantity", "Shares"],
    "proceeds": ["Proceeds", "Sales Proceeds"],
    "cost_basis": ["Cost Basis", "Adjusted Cost Basis", "Cost or Other Basis"],
    "wash_sale_loss": ["Wash Sale Loss Disallowed", "Wash Sale Adjustment"],
    "gain_loss": ["Gain or (Loss)", "Gain/Loss", "Net Gain or Loss"],
    "term": ["Short-term or long-term", "Term", "ST/LT"],
    "covered": ["Covered", "Reported"],
}

VANGUARD_COLUMNS: dict[str, list[str]] = {
    "date_acquired": ["Date acquired", "Acquisition date", "Open date"],
    "date_sold": ["Date sold", "Sale date"],
    "description": ["Investment", "Description", "Fund name"],
    "quantity": ["Shares", "Quantity"],
    "proceeds": ["Gross proceeds", "Proceeds"],
    "cost_basis": ["Cost basis", "Adjusted cost basis"],
    "wash_sale_loss": ["Wash sale loss disallowed", "Wash sale adjustment"],
    "gain_loss": ["Net gain or loss", "Gain/Loss"],
    "term": ["Term", "Short-term/Long-term"],
    "covered": ["Federal tax withheld", "Covered"],  # Vanguard sometimes omits
}

COLUMN_MAPS: dict[str, dict[str, list[str]]] = {
    ETRADE: ETRADE_COLUMNS,
    SCHWAB: SCHWAB_COLUMNS,
    VANGUARD: VANGUARD_COLUMNS,
}

# ---------------------------------------------------------------------------
# Parsed row dataclass
# ---------------------------------------------------------------------------


@dataclass
class BrokerageRow:
    """Normalised representation of a single brokerage transaction row."""

    brokerage: str
    date_sold: str          # YYYY-MM-DD
    date_acquired: str      # YYYY-MM-DD or "Various"
    description: str
    quantity: Decimal | None
    proceeds: Decimal | None
    cost_basis: Decimal | None
    wash_sale_loss: Decimal  # 0 if not present
    gain_loss: Decimal | None
    is_long_term: bool       # True = long-term (>1yr), False = short-term
    covered: str             # "Covered", "Uncovered", or empty
    raw: dict[str, str]      # Original row for raw_data preservation
    row_index: int           # 1-based row number in the CSV (for source_id)


# ---------------------------------------------------------------------------
# Column resolution helper
# ---------------------------------------------------------------------------


def _resolve_column(headers: list[str], candidates: list[str]) -> str | None:
    """Return the first CSV header that matches any candidate (case-insensitive).

    Args:
        headers:    Actual column headers from the CSV.
        candidates: Ordered list of possible column names to try.

    Returns:
        Matched header string or None if no match found.
    """
    lower_headers = {h.strip().lower(): h for h in headers}
    for candidate in candidates:
        match = lower_headers.get(candidate.strip().lower())
        if match is not None:
            return match
    return None


def _get(row: dict[str, str], candidates: list[str]) -> str:
    """Extract a value from a CSV row dict using candidate column names."""
    for candidate in candidates:
        for key in row:
            if key.strip().lower() == candidate.strip().lower():
                v = row[key]
                return v.strip() if v else ""
    return ""


# ---------------------------------------------------------------------------
# Amount parsing
# ---------------------------------------------------------------------------


def _parse_amount(raw: str) -> Decimal | None:
    """Parse a dollar amount string into Decimal.

    Handles:
      - "$1,234.56"  ->  Decimal("1234.56")
      - "(123.45)"   ->  Decimal("-123.45")  (parenthesis = negative)
      - "-123.45"    ->  Decimal("-123.45")
      - ""           ->  None

    Returns None when the string is empty or unparseable.
    """
    if not raw:
        return None
    cleaned = raw.strip().replace("$", "").replace(",", "").replace(" ", "")
    # Parenthesis notation for negatives: (123.45) -> -123.45
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_date(raw: str) -> str | None:
    """Normalise a date string to YYYY-MM-DD.

    Handles:
      - "2024-01-15"       (already ISO)
      - "01/15/2024"       (MM/DD/YYYY)
      - "01/15/24"         (MM/DD/YY)
      - "January 15, 2024" (long form, not handled — returns None)
      - "Various"          (returned as-is for multi-lot basis)

    Returns None for unparseable dates.
    """
    raw = raw.strip()
    if not raw or raw.lower() in ("n/a", "--", ""):
        return None
    if raw.lower() == "various":
        return "Various"
    # Already ISO
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    # MM/DD/YYYY
    parts = raw.replace("-", "/").split("/")
    if len(parts) == 3:
        m, d, y = parts[0], parts[1], parts[2]
        if len(y) == 2:
            y = f"20{y}" if int(y) < 50 else f"19{y}"
        if len(m) == 1:
            m = m.zfill(2)
        if len(d) == 1:
            d = d.zfill(2)
        try:
            # Validate the date makes sense
            int(y), int(m), int(d)
            return f"{y}-{m}-{d}"
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Term classification
# ---------------------------------------------------------------------------

_SHORT_KEYWORDS = {"short", "st", "s", "short-term", "short term"}
_LONG_KEYWORDS = {"long", "lt", "l", "long-term", "long term"}


def _classify_term(term_raw: str, date_acquired: str, date_sold: str) -> bool:
    """Return True for long-term, False for short-term.

    Priority:
      1. Explicit keyword in term_raw ("Short", "Long", "ST", "LT").
      2. Infer from hold period when both dates are parseable ISO dates.
      3. Default to short-term when ambiguous.
    """
    if term_raw:
        t = term_raw.strip().lower()
        if t in _LONG_KEYWORDS:
            return True
        if t in _SHORT_KEYWORDS:
            return False

    # Try date-based inference when term field is absent/ambiguous
    if (
        date_acquired
        and date_sold
        and date_acquired != "Various"
        and len(date_acquired) == 10
        and len(date_sold) == 10
    ):
        try:
            from datetime import date as _date

            acq = _date.fromisoformat(date_acquired)
            sold = _date.fromisoformat(date_sold)
            return (sold - acq).days > 365
        except ValueError:
            pass

    return False  # conservative default: short-term


# ---------------------------------------------------------------------------
# Row-level source_id for dedup
# ---------------------------------------------------------------------------


def _make_source_id(brokerage: str, date_sold: str, description: str, row_index: int) -> str:
    """Build a stable per-row identifier for dedup.

    Uses brokerage + date_sold + description + row_index to handle cases where
    the same security is sold on the same day (different lot = different row_index).
    """
    payload = f"{brokerage}:{date_sold}:{description}:{row_index}"
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Brokerage detection
# ---------------------------------------------------------------------------


def detect_brokerage(content: str) -> str | None:
    """Attempt to auto-detect the brokerage from CSV content.

    Looks for brokerage-specific header signatures in the first few lines.

    Returns one of ETRADE, SCHWAB, VANGUARD, or None.
    """
    header_lines = content[:2000].lower()
    if "e*trade" in header_lines or "etrade" in header_lines:
        return ETRADE
    if "schwab" in header_lines:
        return SCHWAB
    if "vanguard" in header_lines:
        return VANGUARD
    return None


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def find_header_row(lines: list[str]) -> int:
    """Find the index of the row that contains the actual column headers.

    Brokerage CSVs often have preamble rows (account info, disclaimers) before
    the data table. We look for the first row that has multiple comma-separated
    fields and contains at least one expected column keyword.

    Returns the 0-based index of the header row, or 0 if not found.
    """
    keywords = {
        "date", "description", "proceeds", "cost", "gain", "loss",
        "quantity", "shares", "term", "security", "investment", "symbol",
    }
    for i, line in enumerate(lines):
        lower = line.lower()
        if sum(1 for kw in keywords if kw in lower) >= 2:
            return i
    return 0


def parse_brokerage_csv(
    content: str,
    brokerage: str,
    filename: str = "unknown.csv",
) -> list[BrokerageRow]:
    """Parse a brokerage CSV file into a list of BrokerageRow objects.

    Args:
        content:   Raw CSV text content.
        brokerage: One of ETRADE, SCHWAB, VANGUARD.
        filename:  Source filename (used in error messages only).

    Returns:
        List of parsed rows. Rows that cannot be parsed are skipped with a
        warning log (per-record error isolation).

    Raises:
        ValueError: When the brokerage is not supported.
    """
    if brokerage not in SUPPORTED_BROKERAGES:
        raise ValueError(
            f"Unsupported brokerage {brokerage!r}. "
            f"Supported: {SUPPORTED_BROKERAGES}"
        )

    col_map = COLUMN_MAPS[brokerage]

    # ── Find the header row ────────────────────────────────────────────────────
    lines = content.splitlines()
    header_idx = find_header_row(lines)
    data_content = "\n".join(lines[header_idx:])

    reader = csv.DictReader(io.StringIO(data_content))

    rows: list[BrokerageRow] = []
    row_index = 0  # 1-based counter for meaningful rows only

    for raw_row in reader:
        # Skip blank / summary rows (all values empty or a single label)
        non_empty = [v for v in raw_row.values() if v and v.strip()]
        if len(non_empty) < 2:
            continue

        row_index += 1
        record_label = f"{filename}:row{row_index}"

        try:
            # ── Extract fields using column maps ──────────────────────────────
            date_sold_raw = _get(raw_row, col_map["date_sold"])
            date_acq_raw = _get(raw_row, col_map["date_acquired"])
            description = _get(raw_row, col_map["description"]) or "Unknown Security"
            quantity_raw = _get(raw_row, col_map["quantity"])
            proceeds_raw = _get(raw_row, col_map["proceeds"])
            cost_basis_raw = _get(raw_row, col_map["cost_basis"])
            wash_sale_raw = _get(raw_row, col_map["wash_sale_loss"])
            gain_loss_raw = _get(raw_row, col_map["gain_loss"])
            term_raw = _get(raw_row, col_map["term"])
            covered_raw = _get(raw_row, col_map["covered"])

            # ── Parse dates ───────────────────────────────────────────────────
            date_sold = _parse_date(date_sold_raw)
            if not date_sold:
                logger.warning(
                    "%s: unparseable date_sold %r — skipping row",
                    record_label,
                    date_sold_raw,
                )
                continue

            date_acquired = _parse_date(date_acq_raw) or "Various"

            # ── Parse amounts ─────────────────────────────────────────────────
            quantity = _parse_amount(quantity_raw)
            proceeds = _parse_amount(proceeds_raw)
            cost_basis = _parse_amount(cost_basis_raw)
            wash_sale_loss = _parse_amount(wash_sale_raw) or Decimal("0")
            gain_loss = _parse_amount(gain_loss_raw)

            # ── Compute gain/loss when not explicit ───────────────────────────
            if gain_loss is None and proceeds is not None and cost_basis is not None:
                gain_loss = proceeds - cost_basis - wash_sale_loss

            # ── Term classification ───────────────────────────────────────────
            is_long_term = _classify_term(term_raw, date_acquired, date_sold)

            rows.append(
                BrokerageRow(
                    brokerage=brokerage,
                    date_sold=date_sold,
                    date_acquired=date_acquired,
                    description=description.strip(),
                    quantity=quantity,
                    proceeds=proceeds,
                    cost_basis=cost_basis,
                    wash_sale_loss=wash_sale_loss,
                    gain_loss=gain_loss,
                    is_long_term=is_long_term,
                    covered=covered_raw,
                    raw=dict(raw_row),
                    row_index=row_index,
                )
            )

        except Exception as exc:
            logger.warning(
                "%s: failed to parse row — %s",
                record_label,
                exc,
                exc_info=True,
            )
            continue

    return rows


# ---------------------------------------------------------------------------
# Row → Transaction mapping
# ---------------------------------------------------------------------------


def row_to_transaction(row: BrokerageRow, source_id: str, source_hash: str) -> Transaction:
    """Convert a BrokerageRow into a Transaction ORM object.

    Tax category:
      - gain_loss > 0 and long-term  → INVESTMENT_INCOME / CAPITAL_GAIN_LONG
      - gain_loss > 0 and short-term → INVESTMENT_INCOME / CAPITAL_GAIN_SHORT
      - gain_loss <= 0               → INVESTMENT_INCOME (capital loss, still on Sched D)
      - gain_loss is None            → NEEDS_REVIEW

    Amount sign: positive = gain (income), negative = loss (expense/offset).

    Entity defaults to personal (investment accounts are personal).
    """
    # Determine tax category and subcategory
    if row.gain_loss is not None:
        tax_category = TaxCategory.INVESTMENT_INCOME.value
        if row.is_long_term:
            tax_subcategory = TaxSubcategory.CAPITAL_GAIN_LONG.value
        else:
            tax_subcategory = TaxSubcategory.CAPITAL_GAIN_SHORT.value
        # Amount: positive gain is income, negative loss is expense
        amount = row.gain_loss
        direction = Direction.INCOME.value if amount >= 0 else Direction.EXPENSE.value
        status = TransactionStatus.AUTO_CLASSIFIED.value
        confidence = 0.85
        review_reason = None
    else:
        tax_category = None
        tax_subcategory = None
        amount = None
        direction = None
        status = TransactionStatus.NEEDS_REVIEW.value
        confidence = 0.0
        review_reason = "Could not compute gain/loss — cost basis or proceeds missing."

    # Build the raw_data payload (verbatim original plus parsed metadata)
    raw_data: dict[str, Any] = {
        "original_row": row.raw,
        "brokerage": row.brokerage,
        "date_acquired": row.date_acquired,
        "date_sold": row.date_sold,
        "quantity": str(row.quantity) if row.quantity is not None else None,
        "proceeds": str(row.proceeds) if row.proceeds is not None else None,
        "cost_basis": str(row.cost_basis) if row.cost_basis is not None else None,
        "wash_sale_loss_disallowed": str(row.wash_sale_loss),
        "gain_loss": str(row.gain_loss) if row.gain_loss is not None else None,
        "is_long_term": row.is_long_term,
        "covered": row.covered,
        "row_index": row.row_index,
    }

    description = f"{row.brokerage.upper()}: {row.description}"
    if row.date_acquired and row.date_acquired != "Various":
        description += f" (acq {row.date_acquired})"

    return Transaction(
        source=Source.BROKERAGE_CSV.value,
        source_id=source_id,
        source_hash=source_hash,
        date=row.date_sold,
        description=description,
        amount=amount,
        currency="USD",
        entity=Entity.PERSONAL.value,
        direction=direction,
        tax_category=tax_category,
        tax_subcategory=tax_subcategory,
        deductible_pct=0.0,  # Capital gains/losses are reported, not deducted
        status=status,
        confidence=confidence,
        review_reason=review_reason,
        raw_data=raw_data,
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class BrokerageCsvAdapter(BaseAdapter):
    """Ingests brokerage 1099-B CSV data for E*Trade, Schwab, and Vanguard.

    Args:
        csv_content: Raw CSV text content.
        brokerage:   One of 'etrade', 'schwab', 'vanguard'. When None, auto-detect.
        filename:    Original filename (used for logging and source_id generation).
    """

    csv_content: str
    filename: str = "brokerage.csv"
    brokerage: str | None = None

    @property
    def source(self) -> str:
        return Source.BROKERAGE_CSV.value

    def run(self, session: Session) -> AdapterResult:
        """Parse the CSV and insert new Transaction rows.

        Per-record error isolation: a failure on one row does not halt the batch.
        Dedup is enforced via source_hash.
        """
        result = AdapterResult(source=self.source)

        # ── Detect brokerage ───────────────────────────────────────────────────
        brokerage = self.brokerage
        if brokerage is None:
            brokerage = detect_brokerage(self.csv_content)
        if brokerage is None:
            from src.models.enums import IngestionStatus
            result.status = IngestionStatus.FAILURE
            result.errors.append(
                (
                    self.filename,
                    "Could not auto-detect brokerage format. "
                    "Pass brokerage='etrade'|'schwab'|'vanguard' explicitly.",
                )
            )
            return result

        # ── Parse rows ─────────────────────────────────────────────────────────
        try:
            rows = parse_brokerage_csv(self.csv_content, brokerage, self.filename)
        except ValueError as exc:
            from src.models.enums import IngestionStatus
            result.status = IngestionStatus.FAILURE
            result.errors.append((self.filename, str(exc)))
            return result

        logger.info(
            "BrokerageCsvAdapter[%s]: parsed %d rows from %s",
            brokerage,
            len(rows),
            self.filename,
        )

        # ── Insert transactions ────────────────────────────────────────────────
        for row in rows:
            record_label = f"{self.filename}:row{row.row_index}"
            try:
                source_id = _make_source_id(
                    row.brokerage, row.date_sold, row.description, row.row_index
                )
                source_hash = compute_source_hash(self.source, source_id)

                # Dedup check
                existing = (
                    session.query(Transaction)
                    .filter(Transaction.source_hash == source_hash)
                    .first()
                )
                if existing is not None:
                    logger.debug(
                        "Skipping duplicate row: %s (source_id=%s)",
                        record_label,
                        source_id,
                    )
                    result.records_skipped += 1
                    result.records_processed += 1
                    continue

                tx = row_to_transaction(row, source_id, source_hash)
                session.add(tx)
                session.commit()

                result.records_created += 1
                result.records_processed += 1
                logger.info(
                    "Ingested %s  security=%r  date=%s  gain_loss=%s  term=%s",
                    record_label,
                    row.description,
                    row.date_sold,
                    row.gain_loss,
                    "long" if row.is_long_term else "short",
                )

            except Exception as exc:
                result.record_error(record_label, exc)
                result.records_processed += 1
                with contextlib.suppress(Exception):
                    session.rollback()

        return result
