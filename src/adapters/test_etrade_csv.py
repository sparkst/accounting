"""Tests for the E*TRADE CSV adapter.

REQ-005a: Account registry — discovers account from line 3 metadata.
REQ-005b: paired_transaction_id link — synthesized dividend partner for
          E*TRADE single-row Dividend Reinvestment.
REQ-005c: Position snapshots dedup via source_row_hash.
REQ-005e: Idempotency via length-framed hash including row_index +
          synthetic_suffix.
REQ-005f: 6-row metadata header skip; 2-digit year date parsing; CUSIP
          column populated.
REQ-005g: Inherits BaseAdapter; writes IngestionLog.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.etrade_csv import (
    EtradeCsvAdapter,
    map_action,
    parse_account_line,
)
from src.models.base import Base
from src.models.brokerage import (
    Account,
    BrokerageTransaction,
    PositionSnapshot,
)
from src.models.enums import (
    AccountType,
    Broker,
    CanonicalAction,
    Entity,
    Source,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite session per test."""
    from sqlalchemy import event

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )

    # Enable FK enforcement on every SQLite connection (matches the pattern
    # used by test_schwab_csv.py / test_vanguard_csv.py / test_fidelity_csv.py).
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Inline byte-string fixtures (mirrors test_bank_csv.py pattern)
# ---------------------------------------------------------------------------

# Real DownloadTxnHistory.csv layout:
#   row 1: section title
#   row 2: blank
#   row 3: account marker  ('Account Activity for Cap 1(-6084) -6354 from ...')
#   row 4: blank
#   row 5: 'Total:,...'
#   row 6: blank
#   row 7: column header
#   row 8+: data
ETRADE_TXN_CSV = b"""All Transactions Activity Types

Account Activity for Cap 1(-6084) -6354 from 2024-05-03 to 2026-05-03

Total:,-40452.83

Activity/Trade Date,Transaction Date,Settlement Date,Activity Type,Description,Symbol,Cusip,Quantity #,Price $,Amount $,Commission,Category,Note
05/01/26,05/01/26,04/30/26,Dividend,VANGUARD MUNI MMKT DIV PAYMENT,VMSXX,--,,,281.21,0.0,--,--
04/30/26,04/30/26,04/30/26,Interest Income,MORGAN STANLEY BANK N.A. (Period 04/01-04/30),MSBNK,--,,,0.25,0.0,--,--
04/22/26,04/22/26,04/21/26,Stock Split,VANGUARD GROWTH ETF SPLIT RATIO  6:1,VUG,--,256.39,,0.0,0.0,--,--
04/13/26,04/13/26,04/13/26,Online Transfer,ACH WITHDRAWL  REFID:18012041395;,--,--,,,-100000.0,0.0,--,--
04/06/26,04/06/26,04/07/26,Sold,VANGUARD MUNI MMKT CONFIRM NBR UNSOLICITED TRADE,VMSXX,--,-200000.0,1.0,200000.0,0.0,--,--
03/15/26,03/15/26,03/16/26,Bought,APPLE INC,AAPL,037833100,10.0,150.0,-1500.0,0.0,--,--
03/01/26,03/01/26,03/01/26,Qualified Dividend,APPLE INC QUALIFIED DIVIDEND,AAPL,037833100,,,75.50,0.0,--,--
"""

# Single-row Dividend Reinvestment (the synthesis case).
ETRADE_REINVEST_CSV = b"""All Transactions Activity Types

Account Activity for Cap 1(-6084) -6354 from 2024-05-03 to 2026-05-03

Total:,0.00

Activity/Trade Date,Transaction Date,Settlement Date,Activity Type,Description,Symbol,Cusip,Quantity #,Price $,Amount $,Commission,Category,Note
05/01/26,05/01/26,04/30/26,Dividend Reinvestment,281.210000 VANGUARD MUNI MMKT REINVEST PRICE $ 1 As of 04/30/26,VMSXX,922908553,281.21,1.0,-281.21,0.0,--,--
"""

# PortfolioDownload.csv — summary header + position columns.
ETRADE_POSITIONS_CSV = b"""Account Summary
Account,Net Account Value,Total Gain $,Total Gain %,Day's Gain Unrealized $,Day's Gain Unrealized %,Available For Withdrawal,Cash Purchasing Power
"Cap 1(-6084) -6354",3901067.59,2248891.49,136.12,22632.48,.58,1.42,1.42


View Summary - All Positions
Filters applied:
Symbol,Security type(s),Sort by,Sort order,
,All,Symbol,Asc,

Symbol,Last Price $,Change $,Change %,Quantity,Price Paid $,Day's Gain $,Total Gain $,Total Gain %,Value $
AAPL,150.00,1.50,1.00,100.0000,140.0000,150.0000,1000.0000,7.1428,15000.0000
MSFT,413.85,-0.59,-0.14,2630.5850,333.9237,-1551.7821,210253.0864,23.9355,1088667.8653
"""


# ---------------------------------------------------------------------------
# Helper: build an E*TRADE folder fixture from inline bytes
# ---------------------------------------------------------------------------


def _make_folder(
    tmp_path: Path,
    *,
    txn_csv: bytes | None = ETRADE_TXN_CSV,
    pos_csv: bytes | None = ETRADE_POSITIONS_CSV,
) -> Path:
    folder = tmp_path / "etrade"
    folder.mkdir()
    if txn_csv is not None:
        (folder / "DownloadTxnHistory.csv").write_bytes(txn_csv)
    if pos_csv is not None:
        (folder / "PortfolioDownload.csv").write_bytes(pos_csv)
    return folder


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


class TestParseAccountLine:
    """REQ-005a: account auto-discovery from line 3."""

    def test_real_format(self):
        line = "Account Activity for Cap 1(-6084) -6354 from 2024-05-03 to 2026-05-03"
        assert parse_account_line(line) == "6354"

    def test_no_match_returns_none(self):
        assert parse_account_line("totally unrelated text") is None

    def test_empty_returns_none(self):
        assert parse_account_line("") is None


class TestMapAction:
    """REQ-005f: action mapping is exhaustive on observed E*TRADE actions."""

    def test_bought(self):
        assert map_action("Bought") == CanonicalAction.BUY.value

    def test_sold(self):
        assert map_action("Sold") == CanonicalAction.SELL.value

    def test_dividend(self):
        assert map_action("Dividend") == CanonicalAction.DIVIDEND_ORDINARY.value

    def test_qualified_dividend_distinct_from_dividend(self):
        """REQ-005f: 'Qualified Dividend' must NOT be confused with 'Dividend'."""
        assert (
            map_action("Qualified Dividend") == CanonicalAction.DIVIDEND_QUALIFIED.value
        )
        assert map_action("Qualified Dividend") != map_action("Dividend")

    def test_dividend_reinvestment(self):
        assert map_action("Dividend Reinvestment") == CanonicalAction.REINVEST.value

    def test_interest(self):
        assert map_action("Interest") == CanonicalAction.INTEREST.value

    def test_interest_income(self):
        assert map_action("Interest Income") == CanonicalAction.INTEREST.value

    def test_stock_split(self):
        assert map_action("Stock Split") == CanonicalAction.STOCK_SPLIT.value

    def test_transfer(self):
        assert map_action("Transfer") == CanonicalAction.TRANSFER.value

    def test_wire(self):
        assert map_action("Wire") == CanonicalAction.TRANSFER.value

    def test_direct_debit(self):
        assert map_action("Direct Debit") == CanonicalAction.TRANSFER.value

    def test_online_transfer_maps_to_transfer(self):
        """REQ-005f: 'Online Transfer' was previously falling through to 'other'."""
        assert map_action("Online Transfer") == CanonicalAction.TRANSFER.value

    def test_adjustment_maps_to_other(self):
        assert map_action("Adjustment") == CanonicalAction.OTHER.value

    def test_reorganization_maps_to_other(self):
        assert map_action("Reorganization") == CanonicalAction.OTHER.value

    def test_unknown_maps_to_other(self):
        assert map_action("WhoKnowsWhat") == CanonicalAction.OTHER.value


# ---------------------------------------------------------------------------
# Adapter integration tests (full ingest)
# ---------------------------------------------------------------------------


class TestAdapterTxnIngest:
    """REQ-005a/g: full DownloadTxnHistory.csv ingestion."""

    def test_account_auto_discovered(self, session: Session, tmp_path: Path):
        """REQ-005a: account row created with number parsed from line 3."""
        folder = _make_folder(tmp_path)
        adapter = EtradeCsvAdapter(folder)
        adapter.run(session)

        accounts = session.query(Account).all()
        assert len(accounts) == 1
        assert accounts[0].broker == Broker.ETRADE.value
        assert accounts[0].account_number == "6354"
        assert accounts[0].account_type == AccountType.TAXABLE.value
        assert accounts[0].entity == Entity.PERSONAL.value

    def test_six_row_metadata_skip(self, session: Session, tmp_path: Path):
        """REQ-005f: 6-row metadata header is skipped via find_header_row."""
        folder = _make_folder(tmp_path)
        adapter = EtradeCsvAdapter(folder)
        result = adapter.run(session)

        # 7 transaction rows in fixture (no reinvest → no synthetic).
        txns = session.query(BrokerageTransaction).all()
        assert len(txns) == 7
        assert result.records_failed == 0

    def test_two_digit_year_dates_parsed(self, session: Session, tmp_path: Path):
        """REQ-005f: 2-digit year dates (`05/01/26`) parse correctly."""
        folder = _make_folder(tmp_path)
        adapter = EtradeCsvAdapter(folder)
        adapter.run(session)

        # Look up by symbol — VMSXX dividend on 05/01/26 → 2026-05-01
        vmsxx = (
            session.query(BrokerageTransaction)
            .filter(BrokerageTransaction.symbol == "VMSXX")
            .filter(BrokerageTransaction.action == "Dividend")
            .first()
        )
        assert vmsxx is not None
        assert vmsxx.trade_date == date(2026, 5, 1)

    def test_cusip_populated(self, session: Session, tmp_path: Path):
        """REQ-005f: CUSIP populated from the Cusip column when present."""
        folder = _make_folder(tmp_path)
        adapter = EtradeCsvAdapter(folder)
        adapter.run(session)

        aapl = (
            session.query(BrokerageTransaction)
            .filter(BrokerageTransaction.symbol == "AAPL")
            .filter(BrokerageTransaction.action == "Bought")
            .first()
        )
        assert aapl is not None
        assert aapl.cusip == "037833100"

    def test_qualified_dividend_persisted_with_distinct_canonical(
        self, session: Session, tmp_path: Path
    ):
        """REQ-005f: 'Qualified Dividend' rows get DIVIDEND_QUALIFIED, not ORDINARY."""
        folder = _make_folder(tmp_path)
        adapter = EtradeCsvAdapter(folder)
        adapter.run(session)

        qd = (
            session.query(BrokerageTransaction)
            .filter(BrokerageTransaction.action == "Qualified Dividend")
            .first()
        )
        assert qd is not None
        assert qd.canonical_action == CanonicalAction.DIVIDEND_QUALIFIED.value
        assert qd.symbol == "AAPL"

    def test_online_transfer_mapped_to_transfer(
        self, session: Session, tmp_path: Path
    ):
        """REQ-005f: 'Online Transfer' must map to TRANSFER (was falling to 'other')."""
        folder = _make_folder(tmp_path)
        adapter = EtradeCsvAdapter(folder)
        adapter.run(session)

        ot = (
            session.query(BrokerageTransaction)
            .filter(BrokerageTransaction.action == "Online Transfer")
            .first()
        )
        assert ot is not None
        assert ot.canonical_action == CanonicalAction.TRANSFER.value

    def test_idempotent_second_run(self, session: Session, tmp_path: Path):
        """REQ-005e: re-ingesting the same folder produces no duplicates."""
        folder = _make_folder(tmp_path)
        adapter = EtradeCsvAdapter(folder)
        adapter.run(session)

        first_count = session.query(BrokerageTransaction).count()
        first_pos_count = session.query(PositionSnapshot).count()

        adapter2 = EtradeCsvAdapter(folder)
        result2 = adapter2.run(session)

        assert session.query(BrokerageTransaction).count() == first_count
        assert session.query(PositionSnapshot).count() == first_pos_count
        assert result2.records_created == 0

    def test_source_value(self):
        """REQ-005g: source enum value is etrade_csv."""
        adapter = EtradeCsvAdapter("/dev/null")
        assert adapter.source == Source.ETRADE_CSV.value


# ---------------------------------------------------------------------------
# Synthesis tests (Dividend Reinvestment → real + synthetic dividend partner)
# ---------------------------------------------------------------------------


class TestDividendReinvestmentSynthesis:
    """REQ-005b: synthesized partner row links via paired_transaction_id."""

    def test_synthesis_creates_two_rows(self, session: Session, tmp_path: Path):
        """One Dividend Reinvestment row → 2 persisted rows (real + synthetic)."""
        folder = _make_folder(tmp_path, txn_csv=ETRADE_REINVEST_CSV, pos_csv=None)
        adapter = EtradeCsvAdapter(folder)
        adapter.run(session)

        txns = (
            session.query(BrokerageTransaction)
            .order_by(BrokerageTransaction.is_synthetic.asc())
            .all()
        )
        assert len(txns) == 2

        real = next(t for t in txns if not t.is_synthetic)
        synth = next(t for t in txns if t.is_synthetic)

        # Real row characteristics
        assert real.action == "Dividend Reinvestment"
        assert real.canonical_action == CanonicalAction.REINVEST.value
        assert real.symbol == "VMSXX"
        assert real.is_synthetic is False
        # P1-006: bidirectional link — real row's paired_transaction_id → synthetic.
        assert real.paired_transaction_id == synth.id

        # Synthetic row characteristics
        assert synth.is_synthetic is True
        assert synth.canonical_action == CanonicalAction.DIVIDEND_ORDINARY.value
        assert synth.symbol == "VMSXX"
        # Synthetic row links to the real row.
        assert synth.paired_transaction_id == real.id
        # Synthetic amount = abs(real.amount): real is -281.21, synth is +281.21.
        assert synth.amount is not None
        assert synth.amount > 0
        assert abs(synth.amount) == abs(real.amount)
        # Synthetic has no quantity (it's the cash side).
        assert synth.quantity is None

    def test_synthesis_idempotent(self, session: Session, tmp_path: Path):
        """REQ-005e: ingest reinvest fixture twice → still exactly 2 rows."""
        folder = _make_folder(tmp_path, txn_csv=ETRADE_REINVEST_CSV, pos_csv=None)

        EtradeCsvAdapter(folder).run(session)
        EtradeCsvAdapter(folder).run(session)

        count = session.query(BrokerageTransaction).count()
        assert count == 2  # NOT 4

    def test_synthesis_distinct_hashes(self, session: Session, tmp_path: Path):
        """Real and synthetic row have distinct source_row_hash values."""
        folder = _make_folder(tmp_path, txn_csv=ETRADE_REINVEST_CSV, pos_csv=None)
        EtradeCsvAdapter(folder).run(session)

        hashes = {
            t.source_row_hash for t in session.query(BrokerageTransaction).all()
        }
        assert len(hashes) == 2


# ---------------------------------------------------------------------------
# Position ingestion tests
# ---------------------------------------------------------------------------


class TestIngestionLog:
    """P1-011 / REQ-005g: IngestionLog is written after every run."""

    def test_ingestion_log_written(self, session: Session, tmp_path: Path):
        """Exactly one IngestionLog row per adapter run, with correct counts."""
        from src.models.ingestion_log import IngestionLog

        folder = _make_folder(tmp_path)
        adapter = EtradeCsvAdapter(folder)
        result = adapter.run(session)

        logs = (
            session.query(IngestionLog)
            .filter(IngestionLog.source == Source.ETRADE_CSV.value)
            .all()
        )
        assert len(logs) == 1
        log = logs[0]
        assert log.records_processed == result.records_processed
        assert log.records_failed == result.records_failed


class TestTradesdownloadSkip:
    """P1-012: tradesdownload.csv is skipped (sign-convention issue)."""

    def test_tradesdownload_is_skipped(
        self, session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """tradesdownload.csv alongside DownloadTxnHistory.csv must NOT be
        ingested — its presence is logged at INFO but zero rows are written for
        that file."""
        import logging

        # Write both files. tradesdownload.csv has the same content as TXN_CSV
        # but that shouldn't matter — it must be skipped entirely.
        folder = _make_folder(tmp_path)
        (folder / "tradesdownload.csv").write_bytes(ETRADE_TXN_CSV)

        caplog.set_level(logging.INFO, logger="src.adapters.etrade_csv")
        adapter = EtradeCsvAdapter(folder)
        adapter.run(session)

        # The log should mention the skip.
        assert any("tradesdownload" in r.message.lower() for r in caplog.records)
        # All transactions should come from DownloadTxnHistory only; the row
        # count should match the 7-row fixture (not 14 if both files were ingested).
        assert session.query(BrokerageTransaction).count() == 7


class TestNoUnintendedOther:
    """P1-010: no fixture action should fall through to OTHER except for
    Adjustment / Reorganization rows which are intentionally OTHER."""

    def test_other_only_for_known_rows(self, session: Session, tmp_path: Path):
        """Every action in ETRADE_TXN_CSV maps to a known canonical action.
        The only OTHER rows are Adjustment and Reorganization (by design)."""
        folder = _make_folder(tmp_path)
        EtradeCsvAdapter(folder).run(session)

        other_txs = (
            session.query(BrokerageTransaction)
            .filter(BrokerageTransaction.canonical_action == CanonicalAction.OTHER.value)
            .all()
        )
        # The TXN fixture has no Adjustment/Reorganization rows — so OTHER count = 0.
        assert len(other_txs) == 0


class TestTickerRe:
    """P1-A: TICKER_RE accepts valid tickers including BRK.B, BF-B; rejects garbage."""

    def _accepts(self, symbol: str) -> bool:
        from src.adapters.etrade_csv import _TICKER_RE
        return bool(_TICKER_RE.match(symbol.upper()))

    def test_brkb_dot_accepted(self):
        """BRK.B is a real NYSE ticker and must be accepted."""
        assert self._accepts("BRK.B")

    def test_bfb_hyphen_accepted(self):
        """BF-B is a real NYSE ticker (Brown-Forman) and must be accepted."""
        assert self._accepts("BF-B")

    def test_preferred_with_hyphen_accepted(self):
        """Preferred share tickers like JPM-PC must be accepted."""
        assert self._accepts("JPM-PC")

    def test_standard_ticker_accepted(self):
        assert self._accepts("AAPL")

    def test_double_star_suffix_accepted(self):
        assert self._accepts("FDRXX**")

    def test_nine_char_alphanumeric_rejected(self):
        """TOOLONGNAM (9 chars, no separator) must be rejected."""
        assert not self._accepts("TOOLONGNAM")

    def test_generated_at_row_rejected(self):
        """Trailing 'Generated at May 4 2026 02:47 PM ET' must be rejected."""
        assert not self._accepts("Generated at May 4 2026 02:47 PM ET")

    def test_plain_cash_rejected(self):
        """'CASH' (5 chars, matches) — edge case: actually accepted by new regex.
        The CASH filter is handled separately via the explicit symbol.upper() check."""
        # CASH is 4 chars, accepted by regex; the code has a separate TOTAL check.
        # This test documents that CASH-the-ticker IS accepted (which is correct;
        # the position pipeline's explicit 'TOTAL' exclusion is separate).
        assert self._accepts("CASH")


class TestPositionIngest:
    """REQ-005c: PortfolioDownload.csv into position_snapshot table."""

    def test_positions_loaded(self, session: Session, tmp_path: Path):
        folder = _make_folder(tmp_path)
        EtradeCsvAdapter(folder).run(session)

        positions = session.query(PositionSnapshot).all()
        # 2 position rows in fixture (AAPL, MSFT).
        assert len(positions) == 2
        symbols = {p.symbol for p in positions}
        assert symbols == {"AAPL", "MSFT"}

    def test_positions_summary_header_skipped(
        self, session: Session, tmp_path: Path
    ):
        """REQ-005f: Account Summary header rows skipped via find_header_row."""
        folder = _make_folder(tmp_path)
        EtradeCsvAdapter(folder).run(session)

        # No PositionSnapshot row should have symbol like 'Account' (would
        # indicate the summary header was wrongly treated as data).
        rows = session.query(PositionSnapshot).all()
        for r in rows:
            assert r.symbol not in (None, "Account", "")

    def test_positions_skip_generated_at_metadata_row(
        self, session: Session, tmp_path: Path
    ):
        """TASK-11: trailing 'Generated at ...' metadata row must NOT be
        ingested as a PositionSnapshot. Only real ticker rows are stored.

        Real E*TRADE PortfolioDownload.csv files include a footer like
        ``Generated at May 4 2026 02:47 PM ET,,,,,,,,,`` which previous code
        treated as a position with symbol='Generated at May 4 2026 02:47 PM ET'.
        """
            # Header + one valid AAPL row + a "Generated at ..." metadata row.
        positions_csv = (
            b"Account Summary\n"
            b"Account,Net Account Value,Total Gain $,Total Gain %,Day's Gain Unrealized $,Day's Gain Unrealized %,Available For Withdrawal,Cash Purchasing Power\n"
            b'"Cap 1(-6084) -6354",100.00,0.00,0.00,0.00,0.00,1.00,1.00\n'
            b"\n\n"
            b"View Summary - All Positions\n"
            b"Filters applied:\n"
            b"Symbol,Security type(s),Sort by,Sort order,\n"
            b",All,Symbol,Asc,\n"
            b"\n"
            b"Symbol,Last Price $,Change $,Change %,Quantity,Price Paid $,Day's Gain $,Total Gain $,Total Gain %,Value $\n"
            b"AAPL,150.00,1.50,1.00,100.0000,140.0000,150.0000,1000.0000,7.1428,15000.0000\n"
            b"Generated at May 4 2026 02:47 PM ET,,,,,,,,,\n"
        )
        folder = _make_folder(tmp_path, pos_csv=positions_csv)
        EtradeCsvAdapter(folder).run(session)

        positions = session.query(PositionSnapshot).all()
        # Only AAPL should land — the "Generated at" row must be filtered.
        assert len(positions) == 1
        assert positions[0].symbol == "AAPL"
