"""Tests for the bank CSV adapter.

REQ-ID: BANK-CSV-001  Column mapping config persisted and reused.
REQ-ID: BANK-CSV-002  Encoding detection: UTF-8, Latin-1, Windows-1252, BOM.
REQ-ID: BANK-CSV-003  Amount parsing: parenthetical negatives, comma thousands,
                       currency symbols.
REQ-ID: BANK-CSV-004  Date validation: unparseable dates rejected per-record.
REQ-ID: BANK-CSV-005  Cross-reference: existing transactions flagged.
REQ-ID: BANK-CSV-006  Preview returns first 5 rows before commit.

Three bank CSV formats are tested:
  - Chase (single signed Amount column, MM/DD/YYYY dates)
  - Bank of America (Debit/Credit split columns, MM/DD/YYYY dates)
  - BECU (Amount column with parenthetical negatives, M/D/YYYY dates)
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.bank_csv import (
    BankCsvAdapter,
    BankCsvConfig,
    detect_encoding,
    find_cross_reference_matches,
    parse_amount,
    parse_csv_bytes,
)
from src.models.base import Base
from src.models.enums import Source, TransactionStatus
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash

# ---------------------------------------------------------------------------
# Fixtures — DB session
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
# Sample CSV content — three bank formats
# ---------------------------------------------------------------------------

# Format 1: Chase Bank — single signed Amount column, MM/DD/YYYY
CHASE_CSV = b"""Date,Description,Amount,Balance
01/05/2025,Amazon.com,-45.99,1234.56
01/08/2025,Direct Deposit Employer,2500.00,3734.56
01/10/2025,Whole Foods Market,-67.23,3667.33
01/12/2025,Netflix,-15.49,3651.84
01/15/2025,Gas Station Shell,-52.00,3599.84
01/20/2025,Starbucks,-6.75,3593.09
"""

CHASE_CONFIG = BankCsvConfig(
    bank_name="chase_checking",
    date_column="Date",
    date_format="%m/%d/%Y",
    description_column="Description",
    amount_column="Amount",
    balance_column="Balance",
    entity="personal",
    payment_method="Chase ****1234",
)

# Format 2: Bank of America — separate Debit/Credit columns, MM/DD/YYYY
BOA_CSV = b"""Date,Description,Reference Number,Account Number,Amount,Debit,Credit
01/03/2025,ONLINE PAYMENT THANK YOU,,1234,,-300.00,
01/05/2025,AMAZON MKTPLACE PMTS,,1234,,-89.50,
01/07/2025,PAYMENT RECEIVED,,1234,,,300.00
01/09/2025,COSTCO WHOLESALE,,1234,,-142.67,
"""

BOA_CONFIG = BankCsvConfig(
    bank_name="boa_credit",
    date_column="Date",
    date_format="%m/%d/%Y",
    description_column="Description",
    amount_column="",
    debit_column="Debit",
    credit_column="Credit",
    entity="personal",
    payment_method="BofA ****5678",
)

# Format 3: BECU — parenthetical negatives, M/D/YYYY dates, Windows-1252 encoding
BECU_CSV_UTF8 = b"""Date,Description,Amount
1/2/2025,PAYROLL DEPOSIT,3200.00
1/4/2025,SAFEWAY #1234,(78.45)
1/6/2025,ELECTRIC BILL PUGET SOUND ENERGY,(145.00)
1/8/2025,ACH TRANSFER SPARKRY AI LLC,500.00
"""

BECU_CONFIG = BankCsvConfig(
    bank_name="becu_checking",
    date_column="Date",
    date_format="%m/%d/%Y",
    description_column="Description",
    amount_column="Amount",
    entity="personal",
    payment_method=None,
)


# ---------------------------------------------------------------------------
# Tests — parse_amount
# ---------------------------------------------------------------------------


class TestParseAmount:
    def test_plain_positive(self):
        assert parse_amount("1234.56") == Decimal("1234.56")

    def test_plain_negative(self):
        assert parse_amount("-45.99") == Decimal("-45.99")

    def test_comma_thousands(self):
        assert parse_amount("1,234.56") == Decimal("1234.56")

    def test_currency_symbol(self):
        assert parse_amount("$1,234.56") == Decimal("1234.56")

    def test_parenthetical_negative(self):
        assert parse_amount("(78.45)") == Decimal("-78.45")

    def test_parenthetical_with_comma(self):
        assert parse_amount("(1,234.56)") == Decimal("-1234.56")

    def test_parenthetical_with_currency(self):
        assert parse_amount("($145.00)") == Decimal("-145.00")

    def test_empty_returns_none(self):
        assert parse_amount("") is None

    def test_whitespace_returns_none(self):
        assert parse_amount("   ") is None

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_amount("not-a-number")

    def test_zero(self):
        assert parse_amount("0.00") == Decimal("0.00")

    def test_euro_symbol(self):
        assert parse_amount("€123.45") == Decimal("123.45")


# ---------------------------------------------------------------------------
# Tests — detect_encoding
# ---------------------------------------------------------------------------


class TestDetectEncoding:
    def test_utf8_detected(self):
        enc = detect_encoding(b"hello world")
        assert enc in ("utf-8", "ascii")

    def test_utf8_bom_detected(self):
        enc = detect_encoding("\ufeffDate,Amount\n".encode("utf-8-sig"))
        assert enc == "utf-8-sig"

    def test_latin1_data(self):
        # Latin-1 encoded text with non-ASCII char — chardet may not always
        # identify this as latin-1 with very short samples, but detect_encoding
        # must return *something* (not raise), and parse_csv_bytes must handle it.
        data = "Caf\xe9 Receipt,more,data\n".encode("latin-1") * 20  # longer = better detection
        enc = detect_encoding(data)
        assert enc  # must return a non-empty string
        # Verify the encoding label is usable by Python's codec (chardet may
        # guess wrong for short samples — the latin-1 fallback in parse_csv_bytes
        # handles that case).
        with contextlib.suppress(UnicodeDecodeError):
            data.decode(enc)

    def test_windows1252_data(self):
        # Windows-1252 specific chars: smart quotes 0x93 0x94
        data = b"\x93hello\x94"
        enc = detect_encoding(data)
        assert enc  # should return something


# ---------------------------------------------------------------------------
# Tests — parse_csv_bytes: Chase format
# ---------------------------------------------------------------------------


class TestParseCsvChase:
    def test_row_count(self):
        result = parse_csv_bytes(CHASE_CSV, CHASE_CONFIG)
        assert len(result.rows) == 6
        assert len(result.errors) == 0

    def test_headers_stripped(self):
        result = parse_csv_bytes(CHASE_CSV, CHASE_CONFIG)
        assert "Date" in result.headers
        assert "Description" in result.headers

    def test_first_row_values(self):
        result = parse_csv_bytes(CHASE_CSV, CHASE_CONFIG)
        row = result.rows[0]
        assert row.date == "2025-01-05"
        assert row.description == "Amazon.com"
        assert row.amount == Decimal("-45.99")

    def test_income_row_positive(self):
        result = parse_csv_bytes(CHASE_CSV, CHASE_CONFIG)
        # Direct deposit row
        income_row = next(r for r in result.rows if "Direct Deposit" in r.description)
        assert income_row.amount == Decimal("2500.00")

    def test_preview_max_5(self):
        result = parse_csv_bytes(CHASE_CSV, CHASE_CONFIG)
        assert len(result.preview) == 5

    def test_iso_date_format(self):
        result = parse_csv_bytes(CHASE_CSV, CHASE_CONFIG)
        for row in result.rows:
            assert len(row.date) == 10
            assert row.date[4] == "-"
            assert row.date[7] == "-"


# ---------------------------------------------------------------------------
# Tests — parse_csv_bytes: Bank of America format (debit/credit split)
# ---------------------------------------------------------------------------


class TestParseCsvBoa:
    def test_row_count(self):
        result = parse_csv_bytes(BOA_CSV, BOA_CONFIG)
        assert len(result.rows) == 4
        assert len(result.errors) == 0

    def test_debit_becomes_negative(self):
        result = parse_csv_bytes(BOA_CSV, BOA_CONFIG)
        # "ONLINE PAYMENT" has debit -300.00 → stored as -300.00
        row = result.rows[0]
        assert row.amount == Decimal("-300.00")

    def test_credit_becomes_positive(self):
        result = parse_csv_bytes(BOA_CSV, BOA_CONFIG)
        # "PAYMENT RECEIVED" has credit 300.00 → stored as +300.00
        credit_row = next(r for r in result.rows if "PAYMENT RECEIVED" in r.description)
        assert credit_row.amount == Decimal("300.00")


# ---------------------------------------------------------------------------
# Tests — parse_csv_bytes: BECU format (parenthetical negatives)
# ---------------------------------------------------------------------------


class TestParseCsvBecu:
    def test_row_count(self):
        result = parse_csv_bytes(BECU_CSV_UTF8, BECU_CONFIG)
        assert len(result.rows) == 4
        assert len(result.errors) == 0

    def test_parenthetical_negative_parsed(self):
        result = parse_csv_bytes(BECU_CSV_UTF8, BECU_CONFIG)
        safeway = next(r for r in result.rows if "SAFEWAY" in r.description)
        assert safeway.amount == Decimal("-78.45")

    def test_income_positive(self):
        result = parse_csv_bytes(BECU_CSV_UTF8, BECU_CONFIG)
        payroll = next(r for r in result.rows if "PAYROLL" in r.description)
        assert payroll.amount == Decimal("3200.00")

    def test_slash_date_format(self):
        result = parse_csv_bytes(BECU_CSV_UTF8, BECU_CONFIG)
        assert result.rows[0].date == "2025-01-02"


# ---------------------------------------------------------------------------
# Tests — date parsing errors
# ---------------------------------------------------------------------------


class TestDateErrors:
    def test_bad_date_rejected_per_record(self):
        csv_bytes = b"Date,Description,Amount\n13/45/2025,Vendor,100.00\n01/05/2025,Other,50.00\n"
        config = BankCsvConfig(
            bank_name="test",
            date_column="Date",
            date_format="%m/%d/%Y",
            description_column="Description",
            amount_column="Amount",
        )
        result = parse_csv_bytes(csv_bytes, config)
        # Bad date → error, good date → row
        assert len(result.errors) == 1
        assert len(result.rows) == 1
        assert result.errors[0].row_number == 1

    def test_empty_date_rejected(self):
        csv_bytes = b"Date,Description,Amount\n,Vendor,100.00\n"
        config = BankCsvConfig(
            bank_name="test",
            date_column="Date",
            date_format="%m/%d/%Y",
            description_column="Description",
            amount_column="Amount",
        )
        result = parse_csv_bytes(csv_bytes, config)
        assert len(result.errors) == 1


# ---------------------------------------------------------------------------
# Tests — BankCsvConfig persistence
# ---------------------------------------------------------------------------


class TestConfigPersistence:
    def test_save_and_load(self, tmp_path: Path, monkeypatch):
        """Saved config can be loaded back."""
        import src.adapters.bank_csv as bank_csv_module
        monkeypatch.setattr(bank_csv_module, "_CONFIG_DIR", tmp_path / "configs")

        config = BankCsvConfig(
            bank_name="test_bank",
            date_column="Transaction Date",
            date_format="%Y-%m-%d",
            description_column="Memo",
            amount_column="Amount",
            entity="sparkry",
            payment_method="Chase ****9999",
        )
        config.save()

        loaded = BankCsvConfig.load("test_bank")
        assert loaded is not None
        assert loaded.bank_name == "test_bank"
        assert loaded.date_column == "Transaction Date"
        assert loaded.date_format == "%Y-%m-%d"
        assert loaded.entity == "sparkry"
        assert loaded.payment_method == "Chase ****9999"

    def test_load_missing_returns_none(self, tmp_path: Path, monkeypatch):
        import src.adapters.bank_csv as bank_csv_module
        monkeypatch.setattr(bank_csv_module, "_CONFIG_DIR", tmp_path / "configs")
        assert BankCsvConfig.load("nonexistent") is None

    def test_list_saved(self, tmp_path: Path, monkeypatch):
        import src.adapters.bank_csv as bank_csv_module
        monkeypatch.setattr(bank_csv_module, "_CONFIG_DIR", tmp_path / "configs")

        for name in ("alpha", "beta", "gamma"):
            BankCsvConfig(bank_name=name).save()

        saved = BankCsvConfig.list_saved()
        assert set(saved) == {"alpha", "beta", "gamma"}


# ---------------------------------------------------------------------------
# Tests — cross-reference matching
# ---------------------------------------------------------------------------


class TestCrossReference:
    def test_no_match_returns_empty(self, session):
        matches = find_cross_reference_matches(session, "2025-01-05", Decimal("-45.99"), None)
        assert matches == []

    def test_matches_by_date_and_amount(self, session):
        # Insert a transaction that matches amount + date
        tx = Transaction(
            source="stripe",
            source_id="pi_abc",
            source_hash=compute_source_hash("stripe", "pi_abc"),
            date="2025-01-05",
            description="Stripe Payout",
            amount=Decimal("-45.99"),
            raw_data={},
        )
        session.add(tx)
        session.commit()

        matches = find_cross_reference_matches(session, "2025-01-05", Decimal("-45.99"), None)
        assert len(matches) == 1
        assert matches[0] == tx.id

    def test_no_match_different_date(self, session):
        tx = Transaction(
            source="stripe",
            source_id="pi_xyz",
            source_hash=compute_source_hash("stripe", "pi_xyz"),
            date="2025-01-06",
            description="Stripe Payout",
            amount=Decimal("-45.99"),
            raw_data={},
        )
        session.add(tx)
        session.commit()

        matches = find_cross_reference_matches(session, "2025-01-05", Decimal("-45.99"), None)
        assert matches == []


# ---------------------------------------------------------------------------
# Tests — BankCsvAdapter dry_run (preview) mode
# ---------------------------------------------------------------------------


class TestAdapterDryRun:
    def test_dry_run_no_db_writes(self, session):
        adapter = BankCsvAdapter(
            CHASE_CSV,
            CHASE_CONFIG,
            filename="chase_jan_2025.csv",
            dry_run=True,
        )
        result = adapter.run(session)

        # No transactions written in dry_run mode
        count = session.query(Transaction).count()
        assert count == 0
        assert result.records_processed == 6
        assert result.records_failed == 0

    def test_dry_run_parse_errors_counted(self, session):
        bad_csv = b"Date,Description,Amount\n13/45/2025,Bad,100.00\n01/01/2025,Good,50.00\n"
        config = BankCsvConfig(
            bank_name="test",
            date_column="Date",
            date_format="%m/%d/%Y",
            description_column="Description",
            amount_column="Amount",
        )
        adapter = BankCsvAdapter(bad_csv, config, filename="test.csv", dry_run=True)
        result = adapter.run(session)
        assert result.records_failed == 1


# ---------------------------------------------------------------------------
# Tests — BankCsvAdapter commit mode
# ---------------------------------------------------------------------------


class TestAdapterCommit:
    def test_chase_rows_inserted(self, session):
        adapter = BankCsvAdapter(
            CHASE_CSV,
            CHASE_CONFIG,
            filename="chase_jan_2025.csv",
            dry_run=False,
        )
        result = adapter.run(session)

        assert result.records_created == 6
        assert result.records_failed == 0

        txns = session.query(Transaction).all()
        assert len(txns) == 6

    def test_boa_rows_inserted(self, session):
        adapter = BankCsvAdapter(
            BOA_CSV,
            BOA_CONFIG,
            filename="boa_jan_2025.csv",
            dry_run=False,
        )
        result = adapter.run(session)
        assert result.records_created == 4

    def test_becu_rows_inserted(self, session):
        adapter = BankCsvAdapter(
            BECU_CSV_UTF8,
            BECU_CONFIG,
            filename="becu_jan_2025.csv",
            dry_run=False,
        )
        result = adapter.run(session)
        assert result.records_created == 4

    def test_idempotent_second_run_skips(self, session):
        adapter = BankCsvAdapter(
            CHASE_CSV,
            CHASE_CONFIG,
            filename="chase_jan_2025.csv",
            dry_run=False,
        )
        adapter.run(session)

        result2 = adapter.run(session)
        assert result2.records_created == 0
        assert result2.records_skipped == 6

    def test_entity_set_from_config(self, session):
        adapter = BankCsvAdapter(
            CHASE_CSV,
            CHASE_CONFIG,
            filename="chase_jan_2025.csv",
            dry_run=False,
        )
        adapter.run(session)
        txns = session.query(Transaction).all()
        for tx in txns:
            assert tx.entity == "personal"

    def test_payment_method_set_from_config(self, session):
        adapter = BankCsvAdapter(
            CHASE_CSV,
            CHASE_CONFIG,
            filename="chase_jan_2025.csv",
            dry_run=False,
        )
        adapter.run(session)
        txns = session.query(Transaction).all()
        for tx in txns:
            assert tx.payment_method == "Chase ****1234"

    def test_status_is_needs_review(self, session):
        adapter = BankCsvAdapter(
            CHASE_CSV,
            CHASE_CONFIG,
            filename="chase_jan_2025.csv",
            dry_run=False,
        )
        adapter.run(session)
        txns = session.query(Transaction).all()
        for tx in txns:
            assert tx.status == TransactionStatus.NEEDS_REVIEW.value

    def test_raw_data_preserved(self, session):
        adapter = BankCsvAdapter(
            CHASE_CSV,
            CHASE_CONFIG,
            filename="chase_jan_2025.csv",
            dry_run=False,
        )
        adapter.run(session)
        txns = session.query(Transaction).all()
        for tx in txns:
            assert tx.raw_data is not None
            assert "filename" in tx.raw_data
            assert "row" in tx.raw_data

    def test_xref_flags_potential_duplicate(self, session):
        # Insert existing transaction that matches a row in the CSV
        existing_tx = Transaction(
            source="stripe",
            source_id="pi_existing",
            source_hash=compute_source_hash("stripe", "pi_existing"),
            date="2025-01-05",
            description="Amazon stripe payment",
            amount=Decimal("-45.99"),
            payment_method="Chase ****1234",
            raw_data={},
        )
        session.add(existing_tx)
        session.commit()

        adapter = BankCsvAdapter(
            CHASE_CSV,
            CHASE_CONFIG,
            filename="chase_jan_2025.csv",
            dry_run=False,
        )
        adapter.run(session)

        # The row matching date=2025-01-05, amount=-45.99 should have review_reason set
        amazon_tx = (
            session.query(Transaction)
            .filter(
                Transaction.source == Source.BANK_CSV.value,
                Transaction.description == "Amazon.com",
            )
            .first()
        )
        assert amazon_tx is not None
        assert amazon_tx.review_reason is not None
        assert "match" in amazon_tx.review_reason.lower()


# ---------------------------------------------------------------------------
# Tests — whitespace stripping in headers
# ---------------------------------------------------------------------------


class TestHeaderWhitespace:
    def test_headers_with_spaces_stripped(self):
        csv_bytes = b" Date , Description , Amount \n01/01/2025,Test,100.00\n"
        config = BankCsvConfig(
            bank_name="test",
            date_column="Date",
            date_format="%m/%d/%Y",
            description_column="Description",
            amount_column="Amount",
        )
        result = parse_csv_bytes(csv_bytes, config)
        assert len(result.rows) == 1
        assert result.rows[0].description == "Test"

    def test_header_list_stripped(self):
        csv_bytes = b" Date , Description , Amount \n01/01/2025,Test,100.00\n"
        config = BankCsvConfig(
            bank_name="test",
            date_column="Date",
            date_format="%m/%d/%Y",
            description_column="Description",
            amount_column="Amount",
        )
        result = parse_csv_bytes(csv_bytes, config)
        assert all(h == h.strip() for h in result.headers)


# ---------------------------------------------------------------------------
# Tests — encoding variants
# ---------------------------------------------------------------------------


class TestEncodingVariants:
    def test_utf8_bom_parsed(self):
        """UTF-8 BOM file parses correctly."""
        csv_bom = "\ufeffDate,Description,Amount\n01/05/2025,Vendor,100.00\n".encode("utf-8-sig")
        config = BankCsvConfig(
            bank_name="test",
            date_column="Date",
            date_format="%m/%d/%Y",
            description_column="Description",
            amount_column="Amount",
        )
        result = parse_csv_bytes(csv_bom, config)
        assert len(result.rows) == 1
        assert result.rows[0].description == "Vendor"

    def test_latin1_encoded(self):
        """Latin-1 encoded file with accented characters parses correctly."""
        csv_latin1 = "Date,Description,Amount\n01/05/2025,Caf\xe9 Mocha,12.50\n".encode("latin-1")
        config = BankCsvConfig(
            bank_name="test",
            date_column="Date",
            date_format="%m/%d/%Y",
            description_column="Description",
            amount_column="Amount",
        )
        result = parse_csv_bytes(csv_latin1, config)
        assert len(result.rows) == 1
        assert "Caf" in result.rows[0].description


# ---------------------------------------------------------------------------
# Tests — S1-008: source_hash is stable across different row orderings
# ---------------------------------------------------------------------------


class TestSourceHashStability:
    """S1-008: Dedup hash is based on content, not row position."""

    def test_same_transactions_different_order_same_hashes(self, session):
        """Two CSVs with the same transactions in different row order produce
        identical source_hashes, so the second import is fully skipped."""
        csv_v1 = b"""Date,Description,Amount,Balance
01/05/2025,Amazon.com,-45.99,1000.00
01/08/2025,Direct Deposit,2500.00,3500.00
01/10/2025,Whole Foods,-67.23,3432.77
"""
        csv_v2 = b"""Date,Description,Amount,Balance
01/10/2025,Whole Foods,-67.23,3432.77
01/05/2025,Amazon.com,-45.99,1000.00
01/08/2025,Direct Deposit,2500.00,3500.00
"""
        config = BankCsvConfig(
            bank_name="test_stability",
            date_column="Date",
            date_format="%m/%d/%Y",
            description_column="Description",
            amount_column="Amount",
            balance_column="Balance",
        )

        # First import — all 3 rows inserted.
        adapter1 = BankCsvAdapter(csv_v1, config, filename="statement.csv", dry_run=False)
        result1 = adapter1.run(session)
        assert result1.records_created == 3

        # Second import with different row order — all 3 rows skipped as duplicates.
        adapter2 = BankCsvAdapter(csv_v2, config, filename="statement.csv", dry_run=False)
        result2 = adapter2.run(session)
        assert result2.records_created == 0
        assert result2.records_skipped == 3

    def test_different_description_produces_different_hash(self, session):
        """Transactions that differ only in description get distinct hashes."""
        csv_a = b"Date,Description,Amount\n01/05/2025,Vendor A,100.00\n"
        csv_b = b"Date,Description,Amount\n01/05/2025,Vendor B,100.00\n"
        config = BankCsvConfig(
            bank_name="test_distinct",
            date_column="Date",
            date_format="%m/%d/%Y",
            description_column="Description",
            amount_column="Amount",
        )

        adapter_a = BankCsvAdapter(csv_a, config, filename="stmt.csv", dry_run=False)
        result_a = adapter_a.run(session)
        assert result_a.records_created == 1

        adapter_b = BankCsvAdapter(csv_b, config, filename="stmt.csv", dry_run=False)
        result_b = adapter_b.run(session)
        assert result_b.records_created == 1  # distinct description → distinct hash
