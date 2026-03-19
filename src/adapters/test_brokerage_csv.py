"""Tests for the brokerage CSV adapter.

REQ-ID: ADAPTER-BROK-001  Format-specific parsers for E*Trade, Schwab, Vanguard.
REQ-ID: ADAPTER-BROK-002  Cost basis and short/long term stored in raw_data JSON.
REQ-ID: ADAPTER-BROK-003  Wash sale adjustment data imported from 1099-B CSV.
REQ-ID: ADAPTER-BROK-004  Maps to INVESTMENT_INCOME / CAPITAL_GAIN_SHORT / CAPITAL_GAIN_LONG.
REQ-ID: ADAPTER-BROK-005  Each brokerage format has configurable column mappings.
REQ-ID: ADAPTER-BROK-006  Per-record error isolation (bad row does not stop batch).
REQ-ID: ADAPTER-BROK-007  Dedup by source_hash — running twice yields no new rows.

All tests use in-memory SQLite and inline CSV strings.
"""

from __future__ import annotations

import textwrap
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.brokerage_csv import (
    ETRADE,
    SCHWAB,
    VANGUARD,
    BrokerageCsvAdapter,
    BrokerageRow,
    _classify_term,
    _make_source_id,
    _parse_amount,
    _parse_date,
    detect_brokerage,
    parse_brokerage_csv,
    row_to_transaction,
)
from src.models.base import Base
from src.models.enums import (
    Entity,
    Source,
    TaxCategory,
    TaxSubcategory,
    TransactionStatus,
)
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite session per test function."""
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
# Sample CSV fixtures
# ---------------------------------------------------------------------------

ETRADE_CSV = textwrap.dedent("""\
    Account,E*TRADE Securities LLC
    Account Number,XXXX-1234

    Date Sold,Date Acquired,Security Description,Quantity,Proceeds,Cost or Other Basis,Wash Sale Loss Disallowed,Gain or Loss,Term
    01/15/2025,06/10/2023,AAPL APPLE INC,10,$1500.00,$1000.00,$0.00,$500.00,Long
    03/20/2025,03/01/2025,TSLA TESLA INC,5,$2000.00,$2500.00,$0.00,-$500.00,Short
    04/05/2025,Various,VOO VANGUARD S&P 500 ETF,20,$5000.00,$4200.00,$150.00,$650.00,Long
""")

SCHWAB_CSV = textwrap.dedent("""\
    "Charles Schwab & Co., Inc.",,,,,,,,
    "Brokerage Account 1234-5678",,,,,,,,

    Date Sold,Date Acquired,Description,Shares,Proceeds,Cost Basis,Gain or (Loss),Wash Sale Loss Disallowed,Short-term or long-term
    01/20/2025,07/15/2022,MSFT MICROSOFT CORP,8,$3200.00,$2000.00,$1200.00,$0.00,Long-term
    02/10/2025,01/05/2025,AMZN AMAZON INC,3,$900.00,$1100.00,-$200.00,$0.00,Short-term
""")

VANGUARD_CSV = textwrap.dedent("""\
    Vanguard Brokerage Services
    Account: Taxable Brokerage

    Date sold,Date acquired,Investment,Shares,Gross proceeds,Cost basis,Net gain or loss,Wash sale loss disallowed,Term
    02/28/2025,01/15/2023,VTSAX Total Stock Market,15,$4500.00,$3000.00,$1500.00,$0.00,Long
    03/15/2025,03/01/2025,VBTLX Bond Market,25,$2500.00,$2600.00,-$100.00,$0.00,Short
""")

# CSV with a bad row (missing date) to test per-record error isolation
MIXED_CSV = textwrap.dedent("""\
    Date Sold,Date Acquired,Security Description,Quantity,Proceeds,Cost or Other Basis,Wash Sale Loss Disallowed,Gain or Loss,Term
    01/15/2025,06/10/2023,AAPL APPLE INC,10,$1500.00,$1000.00,$0.00,$500.00,Long
    ,06/10/2023,TSLA TESLA INC,5,$2000.00,$2500.00,$0.00,-$500.00,Short
    04/05/2025,Various,VOO VANGUARD S&P 500 ETF,20,$5000.00,$4200.00,$150.00,$650.00,Long
""")

# CSV where gain/loss must be computed from proceeds - cost_basis
COMPUTED_GAIN_CSV = textwrap.dedent("""\
    Date Sold,Date Acquired,Security Description,Quantity,Proceeds,Cost or Other Basis,Wash Sale Loss Disallowed,Term
    05/01/2025,01/01/2024,SPY S&P 500 ETF,10,$3000.00,$2500.00,$0.00,Long
""")

# 1099-B style wash sale CSV
WASH_SALE_CSV = textwrap.dedent("""\
    Date Sold,Date Acquired,Security Description,Quantity,Proceeds,Cost or Other Basis,Wash Sale Loss Disallowed,Gain or Loss,Term
    12/20/2024,12/01/2024,RIVN RIVIAN AUTOMOTIVE,100,$800.00,$1200.00,$200.00,-$200.00,Short
""")


# ---------------------------------------------------------------------------
# Unit tests — parsing helpers
# ---------------------------------------------------------------------------


class TestParseAmount:
    def test_plain_dollar(self) -> None:
        assert _parse_amount("$1,234.56") == Decimal("1234.56")

    def test_no_dollar_sign(self) -> None:
        assert _parse_amount("500.00") == Decimal("500.00")

    def test_negative_parenthesis(self) -> None:
        assert _parse_amount("($500.00)") == Decimal("-500.00")

    def test_negative_dash(self) -> None:
        assert _parse_amount("-$500.00") == Decimal("-500.00")

    def test_empty_string(self) -> None:
        assert _parse_amount("") is None

    def test_zero(self) -> None:
        assert _parse_amount("$0.00") == Decimal("0.00")


class TestParseDate:
    def test_iso_format(self) -> None:
        assert _parse_date("2024-01-15") == "2024-01-15"

    def test_us_format(self) -> None:
        assert _parse_date("01/15/2024") == "2024-01-15"

    def test_us_format_2digit_year(self) -> None:
        assert _parse_date("01/15/25") == "2025-01-15"

    def test_various(self) -> None:
        assert _parse_date("Various") == "Various"

    def test_empty(self) -> None:
        assert _parse_date("") is None

    def test_na(self) -> None:
        assert _parse_date("N/A") is None


class TestClassifyTerm:
    def test_explicit_long(self) -> None:
        assert _classify_term("Long", "", "") is True

    def test_explicit_long_term(self) -> None:
        assert _classify_term("Long-term", "", "") is True

    def test_explicit_lt(self) -> None:
        assert _classify_term("LT", "", "") is True

    def test_explicit_short(self) -> None:
        assert _classify_term("Short", "", "") is False

    def test_explicit_st(self) -> None:
        assert _classify_term("ST", "", "") is False

    def test_inferred_long_from_dates(self) -> None:
        # Held for 2 years → long-term
        assert _classify_term("", "2022-01-01", "2024-01-02") is True

    def test_inferred_short_from_dates(self) -> None:
        # Held for 6 months → short-term
        assert _classify_term("", "2024-01-01", "2024-07-01") is False

    def test_default_short_when_ambiguous(self) -> None:
        assert _classify_term("", "Various", "2024-01-01") is False


class TestDetectBrokerage:
    def test_detects_etrade(self) -> None:
        assert detect_brokerage("E*TRADE Securities LLC\nAccount Number") == ETRADE

    def test_detects_schwab(self) -> None:
        assert detect_brokerage("Charles Schwab & Co., Inc.\nAccount") == SCHWAB

    def test_detects_vanguard(self) -> None:
        assert detect_brokerage("Vanguard Brokerage Services\nAccount") == VANGUARD

    def test_returns_none_for_unknown(self) -> None:
        assert detect_brokerage("Fidelity Investments\nAccount") is None


# ---------------------------------------------------------------------------
# Integration tests — CSV parsing
# ---------------------------------------------------------------------------


class TestParseBrokerageCsv:
    def test_etrade_row_count(self) -> None:
        rows = parse_brokerage_csv(ETRADE_CSV, ETRADE, "etrade_test.csv")
        assert len(rows) == 3

    def test_etrade_gain_long_term(self) -> None:
        rows = parse_brokerage_csv(ETRADE_CSV, ETRADE)
        aapl = rows[0]
        assert aapl.description == "AAPL APPLE INC"
        assert aapl.date_sold == "2025-01-15"
        assert aapl.date_acquired == "2023-06-10"
        assert aapl.proceeds == Decimal("1500.00")
        assert aapl.cost_basis == Decimal("1000.00")
        assert aapl.gain_loss == Decimal("500.00")
        assert aapl.wash_sale_loss == Decimal("0.00")
        assert aapl.is_long_term is True

    def test_etrade_loss_short_term(self) -> None:
        rows = parse_brokerage_csv(ETRADE_CSV, ETRADE)
        tsla = rows[1]
        assert tsla.description == "TSLA TESLA INC"
        assert tsla.gain_loss == Decimal("-500.00")
        assert tsla.is_long_term is False

    def test_etrade_wash_sale(self) -> None:
        rows = parse_brokerage_csv(ETRADE_CSV, ETRADE)
        voo = rows[2]
        assert voo.wash_sale_loss == Decimal("150.00")
        assert voo.date_acquired == "Various"

    def test_schwab_row_count(self) -> None:
        rows = parse_brokerage_csv(SCHWAB_CSV, SCHWAB)
        assert len(rows) == 2

    def test_schwab_long_term(self) -> None:
        rows = parse_brokerage_csv(SCHWAB_CSV, SCHWAB)
        msft = rows[0]
        assert msft.is_long_term is True
        assert msft.gain_loss == Decimal("1200.00")

    def test_schwab_short_term(self) -> None:
        rows = parse_brokerage_csv(SCHWAB_CSV, SCHWAB)
        amzn = rows[1]
        assert amzn.is_long_term is False
        assert amzn.gain_loss == Decimal("-200.00")

    def test_vanguard_row_count(self) -> None:
        rows = parse_brokerage_csv(VANGUARD_CSV, VANGUARD)
        assert len(rows) == 2

    def test_vanguard_long_term(self) -> None:
        rows = parse_brokerage_csv(VANGUARD_CSV, VANGUARD)
        vtsax = rows[0]
        assert vtsax.is_long_term is True
        assert vtsax.gain_loss == Decimal("1500.00")

    def test_per_record_error_isolation(self) -> None:
        """Bad row (missing date) is skipped; other rows are parsed."""
        rows = parse_brokerage_csv(MIXED_CSV, ETRADE, "mixed.csv")
        # Row with empty date_sold is skipped; 2 good rows remain
        assert len(rows) == 2
        assert rows[0].description == "AAPL APPLE INC"
        assert rows[1].description == "VOO VANGUARD S&P 500 ETF"

    def test_gain_computed_from_proceeds_minus_basis(self) -> None:
        """When gain/loss column is absent, compute from proceeds - cost_basis."""
        rows = parse_brokerage_csv(COMPUTED_GAIN_CSV, ETRADE)
        assert len(rows) == 1
        spy = rows[0]
        assert spy.proceeds == Decimal("3000.00")
        assert spy.cost_basis == Decimal("2500.00")
        assert spy.gain_loss == Decimal("500.00")  # 3000 - 2500 - 0

    def test_wash_sale_data_preserved(self) -> None:
        """Wash sale loss disallowed is parsed and stored."""
        rows = parse_brokerage_csv(WASH_SALE_CSV, ETRADE)
        assert len(rows) == 1
        rivn = rows[0]
        assert rivn.wash_sale_loss == Decimal("200.00")
        assert rivn.gain_loss == Decimal("-200.00")

    def test_unsupported_brokerage_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported brokerage"):
            parse_brokerage_csv("header\ndata", "fidelity")

    def test_raw_data_preserved(self) -> None:
        """raw field on BrokerageRow contains original CSV column values."""
        rows = parse_brokerage_csv(ETRADE_CSV, ETRADE)
        aapl = rows[0]
        # The raw dict should contain the original CSV column name
        assert any("Gain" in k or "gain" in k.lower() for k in aapl.raw)


# ---------------------------------------------------------------------------
# Unit tests — row_to_transaction mapping
# ---------------------------------------------------------------------------


def _make_row(
    *,
    brokerage: str = ETRADE,
    date_sold: str = "2025-01-15",
    date_acquired: str = "2023-06-10",
    description: str = "AAPL APPLE INC",
    quantity: Decimal | None = Decimal("10"),
    proceeds: Decimal | None = Decimal("1500"),
    cost_basis: Decimal | None = Decimal("1000"),
    wash_sale_loss: Decimal = Decimal("0"),
    gain_loss: Decimal | None = Decimal("500"),
    is_long_term: bool = True,
    covered: str = "Covered",
    row_index: int = 1,
) -> BrokerageRow:
    return BrokerageRow(
        brokerage=brokerage,
        date_sold=date_sold,
        date_acquired=date_acquired,
        description=description,
        quantity=quantity,
        proceeds=proceeds,
        cost_basis=cost_basis,
        wash_sale_loss=wash_sale_loss,
        gain_loss=gain_loss,
        is_long_term=is_long_term,
        covered=covered,
        raw={"Gain or Loss": str(gain_loss)},
        row_index=row_index,
    )


class TestRowToTransaction:
    def _make_tx(self, row: BrokerageRow) -> Transaction:
        source_id = _make_source_id(
            row.brokerage, row.date_sold, row.description, row.row_index
        )
        source_hash = compute_source_hash(Source.BROKERAGE_CSV.value, source_id)
        return row_to_transaction(row, source_id, source_hash)

    def test_long_term_gain_tax_category(self) -> None:
        row = _make_row(is_long_term=True, gain_loss=Decimal("500"))
        tx = self._make_tx(row)
        assert tx.tax_category == TaxCategory.INVESTMENT_INCOME.value
        assert tx.tax_subcategory == TaxSubcategory.CAPITAL_GAIN_LONG.value

    def test_short_term_gain_tax_category(self) -> None:
        row = _make_row(is_long_term=False, gain_loss=Decimal("200"))
        tx = self._make_tx(row)
        assert tx.tax_category == TaxCategory.INVESTMENT_INCOME.value
        assert tx.tax_subcategory == TaxSubcategory.CAPITAL_GAIN_SHORT.value

    def test_loss_still_investment_income_category(self) -> None:
        row = _make_row(is_long_term=True, gain_loss=Decimal("-300"))
        tx = self._make_tx(row)
        assert tx.tax_category == TaxCategory.INVESTMENT_INCOME.value

    def test_gain_amount_positive(self) -> None:
        row = _make_row(gain_loss=Decimal("500"))
        tx = self._make_tx(row)
        assert tx.amount == Decimal("500")

    def test_loss_amount_negative(self) -> None:
        row = _make_row(gain_loss=Decimal("-300"))
        tx = self._make_tx(row)
        assert tx.amount == Decimal("-300")

    def test_entity_is_personal(self) -> None:
        row = _make_row()
        tx = self._make_tx(row)
        assert tx.entity == Entity.PERSONAL.value

    def test_source_is_brokerage_csv(self) -> None:
        row = _make_row()
        tx = self._make_tx(row)
        assert tx.source == Source.BROKERAGE_CSV.value

    def test_auto_classified_when_gain_known(self) -> None:
        row = _make_row(gain_loss=Decimal("100"))
        tx = self._make_tx(row)
        assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
        assert tx.confidence > 0

    def test_needs_review_when_gain_unknown(self) -> None:
        row = _make_row(gain_loss=None, proceeds=None, cost_basis=None)
        tx = self._make_tx(row)
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value
        assert tx.confidence == 0.0

    def test_raw_data_contains_cost_basis(self) -> None:
        row = _make_row(cost_basis=Decimal("1000"))
        tx = self._make_tx(row)
        assert tx.raw_data["cost_basis"] == "1000"

    def test_raw_data_contains_wash_sale(self) -> None:
        row = _make_row(wash_sale_loss=Decimal("150"))
        tx = self._make_tx(row)
        assert tx.raw_data["wash_sale_loss_disallowed"] == "150"

    def test_raw_data_contains_term(self) -> None:
        row = _make_row(is_long_term=True)
        tx = self._make_tx(row)
        assert tx.raw_data["is_long_term"] is True

    def test_raw_data_contains_original_row(self) -> None:
        row = _make_row()
        tx = self._make_tx(row)
        assert "original_row" in tx.raw_data

    def test_description_includes_brokerage_prefix(self) -> None:
        row = _make_row(brokerage=ETRADE, description="AAPL APPLE INC")
        tx = self._make_tx(row)
        assert "ETRADE" in tx.description
        assert "AAPL APPLE INC" in tx.description

    def test_deductible_pct_zero(self) -> None:
        """Capital gains are reported, not deducted as expenses."""
        row = _make_row()
        tx = self._make_tx(row)
        assert tx.deductible_pct == 0.0


# ---------------------------------------------------------------------------
# Integration tests — BrokerageCsvAdapter.run()
# ---------------------------------------------------------------------------


class TestBrokerageCsvAdapter:
    def test_etrade_inserts_transactions(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=ETRADE_CSV, brokerage=ETRADE, filename="etrade.csv"
        )
        result = adapter.run(session)
        assert result.records_created == 3
        assert result.records_failed == 0
        txns = session.query(Transaction).all()
        assert len(txns) == 3

    def test_schwab_inserts_transactions(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=SCHWAB_CSV, brokerage=SCHWAB, filename="schwab.csv"
        )
        result = adapter.run(session)
        assert result.records_created == 2
        assert result.records_failed == 0

    def test_vanguard_inserts_transactions(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=VANGUARD_CSV, brokerage=VANGUARD, filename="vanguard.csv"
        )
        result = adapter.run(session)
        assert result.records_created == 2
        assert result.records_failed == 0

    def test_dedup_on_second_run(self, session: Session) -> None:
        """Running the same CSV twice yields no new rows on the second pass."""
        adapter = BrokerageCsvAdapter(
            csv_content=ETRADE_CSV, brokerage=ETRADE, filename="etrade.csv"
        )
        first = adapter.run(session)
        assert first.records_created == 3

        second = adapter.run(session)
        assert second.records_created == 0
        assert second.records_skipped == 3

        total = session.query(Transaction).count()
        assert total == 3

    def test_auto_detect_etrade(self, session: Session) -> None:
        """Brokerage is auto-detected from CSV header content."""
        adapter = BrokerageCsvAdapter(
            csv_content=ETRADE_CSV, brokerage=None, filename="etrade.csv"
        )
        result = adapter.run(session)
        assert result.records_created == 3

    def test_auto_detect_schwab(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=SCHWAB_CSV, brokerage=None, filename="schwab.csv"
        )
        result = adapter.run(session)
        assert result.records_created == 2

    def test_auto_detect_vanguard(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=VANGUARD_CSV, brokerage=None, filename="vanguard.csv"
        )
        result = adapter.run(session)
        assert result.records_created == 2

    def test_unknown_brokerage_returns_failure(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content="header,data\nvalue1,value2", brokerage=None, filename="unk.csv"
        )
        result = adapter.run(session)
        from src.models.enums import IngestionStatus
        assert result.status == IngestionStatus.FAILURE
        assert result.records_created == 0

    def test_per_record_error_isolation(self, session: Session) -> None:
        """A bad row (missing date) is skipped; other rows are still ingested."""
        adapter = BrokerageCsvAdapter(
            csv_content=MIXED_CSV, brokerage=ETRADE, filename="mixed.csv"
        )
        result = adapter.run(session)
        # 2 good rows, 1 skipped at parse time (not a records_failed — parse skip)
        assert result.records_created == 2
        assert result.records_failed == 0

    def test_wash_sale_data_in_raw_data(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=WASH_SALE_CSV, brokerage=ETRADE, filename="wash.csv"
        )
        adapter.run(session)
        tx = session.query(Transaction).first()
        assert tx is not None
        assert tx.raw_data["wash_sale_loss_disallowed"] == "200.00"

    def test_tax_category_long_term_gain(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=ETRADE_CSV, brokerage=ETRADE, filename="etrade.csv"
        )
        adapter.run(session)
        # First row is AAPL, long-term gain
        txns = (
            session.query(Transaction)
            .filter(Transaction.description.like("%AAPL%"))
            .all()
        )
        assert len(txns) == 1
        assert txns[0].tax_category == TaxCategory.INVESTMENT_INCOME.value
        assert txns[0].tax_subcategory == TaxSubcategory.CAPITAL_GAIN_LONG.value

    def test_tax_category_short_term_gain(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=ETRADE_CSV, brokerage=ETRADE, filename="etrade.csv"
        )
        adapter.run(session)
        tsla = (
            session.query(Transaction)
            .filter(Transaction.description.like("%TSLA%"))
            .first()
        )
        assert tsla is not None
        assert tsla.tax_subcategory == TaxSubcategory.CAPITAL_GAIN_SHORT.value

    def test_entity_personal_on_all_rows(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=ETRADE_CSV, brokerage=ETRADE, filename="etrade.csv"
        )
        adapter.run(session)
        txns = session.query(Transaction).all()
        assert all(tx.entity == "personal" for tx in txns)

    def test_source_hash_unique(self, session: Session) -> None:
        """Each row gets a unique source_hash."""
        adapter = BrokerageCsvAdapter(
            csv_content=ETRADE_CSV, brokerage=ETRADE, filename="etrade.csv"
        )
        adapter.run(session)
        txns = session.query(Transaction).all()
        hashes = [tx.source_hash for tx in txns]
        assert len(hashes) == len(set(hashes))

    def test_date_stored_as_iso(self, session: Session) -> None:
        adapter = BrokerageCsvAdapter(
            csv_content=ETRADE_CSV, brokerage=ETRADE, filename="etrade.csv"
        )
        adapter.run(session)
        txns = session.query(Transaction).all()
        for tx in txns:
            assert len(tx.date) == 10
            assert tx.date[4] == "-" and tx.date[7] == "-"
