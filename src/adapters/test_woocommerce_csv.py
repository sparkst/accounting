"""Tests for the WooCommerce CSV adapter.

REQ-ID: WOOCOMMERCE-CSV-001  Parse WooCommerce order export CSV with standard columns.
REQ-ID: WOOCOMMERCE-CSV-002  Per-record error isolation.
REQ-ID: WOOCOMMERCE-CSV-003  Dedup via SHA256(source, order_number).
REQ-ID: WOOCOMMERCE-CSV-004  Maps to entity=blackline, tax_category=SALES_INCOME,
                              direction=income.
REQ-ID: WOOCOMMERCE-CSV-005  Non-importable statuses skipped.

Fixtures:
  STANDARD_CSV   — WooCommerce default export columns.
  SPLIT_NAME_CSV — Separate billing first/last name columns.
  BAD_ROWS_CSV   — Mix of good rows and per-record errors.
  STATUS_CSV     — Rows with various order statuses.
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.woocommerce_csv import (
    WooCommerceCsvAdapter,
    _parse_date,
    _parse_decimal,
    parse_woocommerce_csv,
)
from src.models.base import Base
from src.models.enums import Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite session per test."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Sample CSV data
# ---------------------------------------------------------------------------

# Standard WooCommerce export columns
STANDARD_CSV = b"""Order Number,Order Date,Order Status,Order Total,Payment Method,Customer Name
1001,2026-01-15,completed,89.99,PayPal,Alice Smith
1002,2026-01-20,completed,149.50,Stripe,Bob Jones
1003,2026-02-03,processing,34.00,Credit Card,Carol White
"""

# Separate billing first/last name columns (no Customer Name)
SPLIT_NAME_CSV = b"""Order Number,Order Date,Order Status,Order Total,Payment Method,Billing First Name,Billing Last Name
2001,2026-01-10,completed,55.00,PayPal,Dave,Brown
2002,2026-02-14,completed,120.00,Stripe,Eve,Davis
"""

# Rows with various order statuses
STATUS_CSV = b"""Order Number,Order Date,Order Status,Order Total,Payment Method
3001,2026-01-01,completed,100.00,PayPal
3002,2026-01-02,pending,25.00,Stripe
3003,2026-01-03,cancelled,50.00,Credit Card
3004,2026-01-04,refunded,75.00,PayPal
3005,2026-01-05,processing,200.00,Stripe
3006,2026-01-06,wc-completed,60.00,Credit Card
"""

# Mix of good rows and per-record errors
BAD_ROWS_CSV = b"""Order Number,Order Date,Order Status,Order Total,Payment Method
4001,2026-01-10,completed,99.00,PayPal
4002,NOT-A-DATE,completed,50.00,Stripe
4003,2026-01-12,completed,NOTANUMBER,PayPal
4004,2026-01-13,completed,-10.00,Stripe
4005,2026-01-14,completed,75.00,Credit Card
"""

# Missing required columns
MISSING_DATE_CSV = b"""Order Number,Order Status,Order Total
5001,completed,100.00
"""

# Currency symbols and comma thousands in amounts
CURRENCY_AMOUNTS_CSV = (
    b"Order Number,Order Date,Order Status,Order Total,Payment Method\r\n"
    b'6001,2026-03-01,completed,"$1,234.56",PayPal\r\n'
    b"6002,2026-03-02,completed,\xc2\xa399.99,Stripe\r\n"
)

# UTF-8 BOM prefix
UTF8_BOM_CSV = b"\xef\xbb\xbf" + b"""Order Number,Order Date,Order Status,Order Total
7001,2026-01-05,completed,45.00
"""


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


class TestParseDecimal:
    def test_plain(self) -> None:
        assert _parse_decimal("89.99") == Decimal("89.99")

    def test_currency_symbol(self) -> None:
        assert _parse_decimal("$1,234.56") == Decimal("1234.56")

    def test_pound_symbol(self) -> None:
        assert _parse_decimal("£99.99") == Decimal("99.99")

    def test_comma_thousands(self) -> None:
        assert _parse_decimal("1,234.56") == Decimal("1234.56")

    def test_blank_returns_none(self) -> None:
        assert _parse_decimal("") is None
        assert _parse_decimal("   ") is None

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse amount"):
            _parse_decimal("notanumber")


class TestParseDate:
    def test_iso_format(self) -> None:
        assert _parse_date("2026-01-15") == "2026-01-15"

    def test_us_format(self) -> None:
        assert _parse_date("01/15/2026") == "2026-01-15"

    def test_datetime_with_time(self) -> None:
        assert _parse_date("2026-01-15T10:30:00") == "2026-01-15"

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse date"):
            _parse_date("NOT-A-DATE")


# ---------------------------------------------------------------------------
# Unit tests: parse_woocommerce_csv
# ---------------------------------------------------------------------------


class TestParseWoocommerceCsv:
    def test_standard_csv_parses_all_rows(self) -> None:
        result = parse_woocommerce_csv(STANDARD_CSV)
        assert len(result.rows) == 3
        assert len(result.errors) == 0

    def test_standard_csv_row_values(self) -> None:
        result = parse_woocommerce_csv(STANDARD_CSV)
        row = result.rows[0]
        assert row.order_number == "1001"
        assert row.date == "2026-01-15"
        assert row.status == "completed"
        assert row.total == Decimal("89.99")
        assert row.payment_method == "PayPal"
        assert row.customer_name == "Alice Smith"

    def test_split_name_columns(self) -> None:
        result = parse_woocommerce_csv(SPLIT_NAME_CSV)
        assert len(result.rows) == 2
        assert result.rows[0].customer_name == "Dave Brown"
        assert result.rows[1].customer_name == "Eve Davis"

    def test_status_filtering(self) -> None:
        result = parse_woocommerce_csv(STATUS_CSV)
        # completed(3001), processing(3005), wc-completed(3006) are importable
        importable_orders = {r.order_number for r in result.rows}
        assert "3001" in importable_orders
        assert "3005" in importable_orders
        assert "3006" in importable_orders
        # pending, cancelled, refunded are skipped
        assert "3002" not in importable_orders
        assert "3003" not in importable_orders
        assert "3004" not in importable_orders

    def test_skipped_statuses_recorded(self) -> None:
        result = parse_woocommerce_csv(STATUS_CSV)
        skipped_row_numbers = {rn for rn, _ in result.skipped_statuses}
        assert 2 in skipped_row_numbers  # pending
        assert 3 in skipped_row_numbers  # cancelled
        assert 4 in skipped_row_numbers  # refunded

    def test_per_record_error_isolation(self) -> None:
        result = parse_woocommerce_csv(BAD_ROWS_CSV)
        # Good rows: 4001 and 4005
        good_orders = {r.order_number for r in result.rows}
        assert "4001" in good_orders
        assert "4005" in good_orders
        # Bad rows: bad date, bad total, negative total
        assert len(result.errors) == 3
        error_row_numbers = {e.row_number for e in result.errors}
        assert 2 in error_row_numbers  # bad date
        assert 3 in error_row_numbers  # bad total
        assert 4 in error_row_numbers  # negative total

    def test_currency_symbols_and_commas(self) -> None:
        result = parse_woocommerce_csv(CURRENCY_AMOUNTS_CSV)
        assert len(result.errors) == 0
        assert result.rows[0].total == Decimal("1234.56")
        assert result.rows[1].total == Decimal("99.99")

    def test_utf8_bom_stripped(self) -> None:
        result = parse_woocommerce_csv(UTF8_BOM_CSV)
        assert len(result.rows) == 1
        assert result.rows[0].order_number == "7001"

    def test_missing_date_column_raises(self) -> None:
        with pytest.raises(ValueError, match="missing a date column"):
            parse_woocommerce_csv(MISSING_DATE_CSV)

    def test_empty_file_returns_empty_result(self) -> None:
        result = parse_woocommerce_csv(b"")
        assert result.rows == []
        assert result.errors == []

    def test_headers_captured(self) -> None:
        result = parse_woocommerce_csv(STANDARD_CSV)
        assert "Order Number" in result.headers
        assert "Order Total" in result.headers


# ---------------------------------------------------------------------------
# Adapter tests
# ---------------------------------------------------------------------------


class TestWooCommerceCsvAdapter:
    def test_dry_run_does_not_persist(self, session: Session) -> None:
        adapter = WooCommerceCsvAdapter(STANDARD_CSV, dry_run=True)
        result = adapter.run(session)
        assert session.query(Transaction).count() == 0
        assert result.records_created == 0

    def test_commit_inserts_transactions(self, session: Session) -> None:
        adapter = WooCommerceCsvAdapter(STANDARD_CSV, dry_run=False)
        result = adapter.run(session)
        assert result.records_created == 3
        assert result.records_failed == 0
        txns = session.query(Transaction).all()
        assert len(txns) == 3

    def test_transaction_fields(self, session: Session) -> None:
        adapter = WooCommerceCsvAdapter(STANDARD_CSV, dry_run=False)
        adapter.run(session)
        tx = session.query(Transaction).filter(
            Transaction.description.like("%1001%")
        ).first()
        assert tx is not None
        assert tx.entity == Entity.BLACKLINE.value
        assert tx.direction == Direction.INCOME.value
        assert tx.tax_category == TaxCategory.SALES_INCOME.value
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value
        assert tx.source == Source.WOOCOMMERCE_CSV.value
        assert tx.amount == Decimal("89.99")
        assert tx.date == "2026-01-15"

    def test_dedup_skips_existing_orders(self, session: Session) -> None:
        adapter = WooCommerceCsvAdapter(STANDARD_CSV, dry_run=False)
        result1 = adapter.run(session)
        assert result1.records_created == 3

        # Re-run same file — all rows should be skipped
        adapter2 = WooCommerceCsvAdapter(STANDARD_CSV, dry_run=False)
        result2 = adapter2.run(session)
        assert result2.records_created == 0
        assert result2.records_skipped == 3

    def test_source_is_woocommerce_csv(self, session: Session) -> None:
        adapter = WooCommerceCsvAdapter(STANDARD_CSV, dry_run=False)
        adapter.run(session)
        assert adapter.source == Source.WOOCOMMERCE_CSV.value
        tx = session.query(Transaction).first()
        assert tx is not None
        assert tx.source == Source.WOOCOMMERCE_CSV.value

    def test_non_importable_statuses_not_inserted(self, session: Session) -> None:
        adapter = WooCommerceCsvAdapter(STATUS_CSV, dry_run=False)
        result = adapter.run(session)
        # Only completed(3001), processing(3005), wc-completed(3006) inserted
        assert result.records_created == 3
        txns = session.query(Transaction).all()
        order_numbers = {
            tx.raw_data["order_number"] for tx in txns if tx.raw_data  # type: ignore[index]
        }
        assert "3001" in order_numbers
        assert "3005" in order_numbers
        assert "3006" in order_numbers
        assert "3002" not in order_numbers  # pending
        assert "3003" not in order_numbers  # cancelled

    def test_bad_rows_isolated(self, session: Session) -> None:
        adapter = WooCommerceCsvAdapter(BAD_ROWS_CSV, dry_run=False)
        result = adapter.run(session)
        # 4001 and 4005 succeed; 3 rows fail
        assert result.records_created == 2
        assert result.records_failed == 3

    def test_customer_name_in_description(self, session: Session) -> None:
        adapter = WooCommerceCsvAdapter(STANDARD_CSV, dry_run=False)
        adapter.run(session)
        tx = session.query(Transaction).filter(
            Transaction.description.like("%Alice Smith%")
        ).first()
        assert tx is not None

    def test_source_hash_uniqueness(self, session: Session) -> None:
        """Different filenames produce different source_hashes for the same order number."""
        adapter1 = WooCommerceCsvAdapter(STANDARD_CSV, filename="export_jan.csv", dry_run=False)
        adapter2 = WooCommerceCsvAdapter(STANDARD_CSV, filename="export_feb.csv", dry_run=False)
        adapter1.run(session)
        result2 = adapter2.run(session)
        # Different filenames → different source_ids → different hashes → all inserted
        assert result2.records_created == 3
        assert session.query(Transaction).count() == 6
