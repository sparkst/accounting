"""Tests for the Schwab brokerage CSV adapter (TASK-07, REQ-005a..g).

REQ-005a: Account upsert keyed on (broker, account_number).
REQ-005b: Brokerage transaction storage with paired_transaction_id linking
          dividend ↔ reinvestment.
REQ-005c: Position snapshots with row-index dedup.
REQ-005d: Realized G/L with wash-sale and term split.
REQ-005e: Idempotent re-ingest via length-framed source_row_hash.
REQ-005f: Tolerant CSV parsing — currency, "as of" dates, comma-thousands.
REQ-005g: Inherits BaseAdapter, returns AdapterResult.

Inline byte-string fixtures matching the project's existing
``test_bank_csv.py`` pattern.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.schwab_csv import (
    SchwabCsvAdapter,
    classify_file,
    default_account_type,
    map_schwab_action,
    parse_positions_metadata,
)
from src.models.base import Base
from src.models.brokerage import (
    Account,
    BrokerageTransaction,
    PositionSnapshot,
    RealizedGainLoss,
)
from src.models.enums import (
    AccountType,
    Broker,
    CanonicalAction,
    GainLossTerm,
    IngestionStatus,
    Source,
)

# ── Session fixture ───────────────────────────────────────────────────────────


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite session per test, with FK enforcement on."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


# ── Fixture CSVs ─────────────────────────────────────────────────────────────

# Joint Tenant transactions: covers the reinvest-dividend pairing case
# (Reinvest Dividend immediately followed by Reinvest Shares for same symbol),
# the "as of" date split, the $-currency parse, and a 1,471 quantity comma.
JOINT_TENANT_TX_CSV = b'''"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"
"04/30/2026","Reinvest Dividend","SWVXX","SCHWAB PRIME ADVANTAGE MONEY","","","","$28.38"
"04/30/2026","Reinvest Shares","SWVXX","SCHWAB PRIME ADVANTAGE MONEY","28.38","$1.00","","-$28.38"
"04/21/2026","Sell","VUG","VANGUARD GROWTH ETF","-0.03","$82.73","","$2.73"
"02/07/2024 as of 02/05/2024","Sell","AMZN","AMAZON.COM INC","-1,471","$170.07","","$250,178.09"
"01/15/2026","Bank Interest","","SCHWAB BANK INTEREST","","","","$5.42"
'''

# AMZN RSU transactions — exercises Journaled Shares → rsu_vest mapping
# AND the same-day duplicate case: two -45 share Journaled Shares rows
# on 12/11/2025 must both persist with distinct hashes.
AMZN_RSU_TX_CSV = b'''"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"
"12/11/2025","Journaled Shares","AMZN","AMAZON.COM INC","-45","$230.28","",""
"12/11/2025","Journaled Shares","AMZN","AMAZON.COM INC","-45","$230.28","",""
"06/06/2022 as of 06/03/2022","Stock Split","AMZN","AMAZON.COM INC","4,009","$122.35","",""
'''

# Joint Tenant positions — exercises the metadata-row parser, the
# title+blank+header skip, and the trailing-row filter.
JOINT_TENANT_POS_CSV = b'''"Positions for account Joint Tenant ...724 as of 01:17 PM ET, 2026/05/03"

"Symbol","Description","Qty (Quantity)","Price","Price Chng % (Price Change %)","Price Chng $ (Price Change $)","Mkt Val (Market Value)","Day Chng $ (Day Change $)","Day Chng % (Day Change %)","Cost Basis","Gain $ (Gain/Loss $)","Gain % (Gain/Loss %)","Ratings","Reinvest?","Reinvest Capital Gains?","% of Acct (% of Account)","Asset Type",
"AMD","ADVANCED MICRO DEVIC","16","360.54","1.71%","6.05","$5,768.64","$96.80","1.71%","$3,073.28","$2,695.36","87.7%","F","No","N/A","0.28%","Equity",
"GOOG","ALPHABET INC CLASS C","201.6059","383.22","0.34%","1.28","$77,259.41","$258.06","0.34%","$11,011.86","$66,247.55","601.6%","B","Yes","N/A","3.75%","Equity",
"Cash & Cash Investments","--","--","--","--","--","$0.95","$0.00","0%","--","--","--","--","--","--","0%","Cash and Money Market",
"Positions Total","","--","--","--","--","$83,028.00","$354.86","0.45%","$14,085.14","$68,942.91","489.5%","--","--","--","--","--",
'''

# AMZN RSU positions — single row + trailing summary rows.
AMZN_RSU_POS_CSV = b'''"Positions for account AMZN RSU ...144 as of 01:17 PM ET, 2026/05/03"

"Symbol","Description","Qty (Quantity)","Price","Price Chng % (Price Change %)","Price Chng $ (Price Change $)","Mkt Val (Market Value)","Day Chng $ (Day Change $)","Day Chng % (Day Change %)","Cost Basis","Gain $ (Gain/Loss $)","Gain % (Gain/Loss %)","Ratings","Reinvest?","Reinvest Capital Gains?","% of Acct (% of Account)","Asset Type",
"AMZN","AMAZON.COM INC","1,347","268.26","1.21%","3.20","$361,346.22","$4,310.40","1.21%","$24,759.91","$336,586.31","1359.4%","C","No","N/A","100%","Equity",
"Cash & Cash Investments","--","--","--","--","--","$0.95","$0.00","0%","--","--","--","--","--","--","0%","Cash and Money Market",
"Positions Total","","--","--","--","--","$361,347.17","$4,310.40","1.19%","$24,759.91","$336,586.31","1359.4%","--","--","--","--","--",
'''

# Joint Tenant realized G/L — exercises wash-sale flag, term split, and the
# "skip first row, header is row 2" structure.
JOINT_TENANT_GL_CSV = b'''"Realized Gain/Loss - Lot Details for Joint_Tenant as of Sun May 03  13:18:19 EDT 2026 from 01/01/2024 to 05/03/2026","","","","","","","","","","","","","","","","","","","","","","","",""
"Symbol","Name","Closed Date","Opened Date","Quantity","Proceeds Per Share","Cost Per Share","Proceeds","Cost Basis (CB)","Gain/Loss ($)","Gain/Loss (%)","Long Term Gain/Loss","Short Term Gain/Loss","Term","Unadjusted Cost Basis","Wash Sale?","Disallowed Loss","Transaction Closed Date","Transaction Cost Basis","Total Transaction Gain/Loss ($)","Total Transaction Gain/Loss (%)","LT Transaction Gain/Loss ($)","LT Transaction Gain/Loss (%)","ST Transaction Gain/Loss ($)","ST Transaction Gain/Loss (%)"
"VUG","VANGUARD GROWTH ETF","04/21/2026","11/23/2016","0.03","$82.73","$18.48","$2.73","$0.61","$2.12","347.54%","$2.12","","Long Term","$0.61","No","","04/21/2026","","","","","","",""
"DBGI","DIGITAL BRANDS GROUP","01/13/2026","01/12/2026","200","$17.00","$15.00","$3,399.96","$3,000.00","$399.96","13.33%","","$399.96","Short Term","$3,000.00","Yes","-$50.00","01/13/2026","","","","","","",""
'''

# 1099 form file — must be skipped entirely.
SCHWAB_1099_CSV = b'''Account,XXXX-X724
Tax Year,2025

Form,Box,Amount
1099-DIV,1a,1234.56
'''

# Qual Div Reinvest pair fixture — Qual Div Reinvest (cash) followed by
# Reinvest Shares (buy); they must be linked via paired_transaction_id.
QUAL_DIV_REINVEST_TX_CSV = b'''"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"
"04/30/2026","Qual Div Reinvest","VOO","VANGUARD S&P 500 ETF","","","","$50.00"
"04/30/2026","Reinvest Shares","VOO","VANGUARD S&P 500 ETF","0.10","$500.00","","-$50.00"
'''

# AMZN RSU realized G/L fixture — exercises RSU account G/L parsing.
AMZN_RSU_GL_CSV = b'''"Realized Gain/Loss for AMZN RSU as of 2026/05/03","","","","","","","","","","","","","","","","","","","","","","","",""
"Symbol","Name","Closed Date","Opened Date","Quantity","Proceeds Per Share","Cost Per Share","Proceeds","Cost Basis (CB)","Gain/Loss ($)","Gain/Loss (%)","Long Term Gain/Loss","Short Term Gain/Loss","Term","Unadjusted Cost Basis","Wash Sale?","Disallowed Loss","Transaction Closed Date","Transaction Cost Basis","Total Transaction Gain/Loss ($)","Total Transaction Gain/Loss (%)","LT Transaction Gain/Loss ($)","LT Transaction Gain/Loss (%)","ST Transaction Gain/Loss ($)","ST Transaction Gain/Loss (%)"
"AMZN","AMAZON.COM INC","12/11/2025","06/03/2022","45","$230.28","$6.19","$10362.60","$278.55","$10084.05","3619.18%","$10084.05","","Long Term","$278.55","No","","12/11/2025","","","","","","",""
'''


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_folder(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """Write a {filename: bytes} dict to a fresh tmp folder."""
    folder = tmp_path / "schwab"
    folder.mkdir()
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder


# ── File classification ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Joint_Tenant_XXX724_Transactions_20260503-131620.csv", "transactions"),
        ("AMZN_RSU_XXX144_Transactions_20260503-131654.csv", "transactions"),
        ("Joint Tenant-Positions-2026-05-03-131725.csv", "positions"),
        ("AMZN RSU-Positions-2026-05-03-131716.csv", "positions"),
        (
            "Joint_Tenant_GainLoss_Realized_Details_20260503-131819.csv",
            "gainloss",
        ),
        ("XXXX-X724 (1).CSV", "1099"),
        ("XXXX-X724 (2).CSV", "1099"),
        ("xxxx-x724.CSV", "1099"),
        ("random.csv", "unknown"),
    ],
)
def test_classify_file(filename: str, expected: str) -> None:
    """REQ-005f: file-kind detection from filename."""
    assert classify_file(Path(filename)) == expected


# ── default_account_type ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("AMZN RSU", AccountType.RSU.value),
        ("amzn rsu", AccountType.RSU.value),
        ("Joint Tenant", AccountType.JOINT.value),
        ("JOINT TENANT", AccountType.JOINT.value),
        ("Individual Brokerage", AccountType.TAXABLE.value),
    ],
)
def test_default_account_type(name: str, expected: str) -> None:
    assert default_account_type(name) == expected


# ── parse_positions_metadata ──────────────────────────────────────────────────


def test_parse_positions_metadata_amzn() -> None:
    line = "Positions for account AMZN RSU ...144 as of 01:17 PM ET, 2026/05/03"
    name, acct, as_of = parse_positions_metadata(line)
    assert name == "AMZN RSU"
    assert acct == "144"
    assert as_of == datetime(2026, 5, 3)


def test_parse_positions_metadata_joint() -> None:
    line = "Positions for account Joint Tenant ...724 as of 01:17 PM ET, 2026/05/03"
    name, acct, as_of = parse_positions_metadata(line)
    assert name == "Joint Tenant"
    assert acct == "724"
    assert as_of == datetime(2026, 5, 3)


def test_parse_positions_metadata_unrecognized() -> None:
    name, acct, as_of = parse_positions_metadata("garbage")
    assert (name, acct, as_of) == ("", "", None)


# ── Action mapping ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "action,account_type,expected",
    [
        ("Buy", AccountType.TAXABLE.value, CanonicalAction.BUY.value),
        ("Buy to Open", AccountType.TAXABLE.value, CanonicalAction.BUY.value),
        ("Sell", AccountType.TAXABLE.value, CanonicalAction.SELL.value),
        ("Sell to Close", AccountType.TAXABLE.value, CanonicalAction.SELL.value),
        ("Reinvest Dividend", AccountType.TAXABLE.value, CanonicalAction.DIVIDEND_ORDINARY.value),
        ("Reinvest Shares", AccountType.TAXABLE.value, CanonicalAction.REINVEST.value),
        ("Qual Div Reinvest", AccountType.TAXABLE.value, CanonicalAction.DIVIDEND_QUALIFIED.value),
        ("Pr Yr Div Reinvest", AccountType.TAXABLE.value, CanonicalAction.DIVIDEND_ORDINARY.value),
        ("Long Term Cap Gain Reinvest", AccountType.TAXABLE.value, CanonicalAction.CAPITAL_GAIN_LT.value),
        ("Short Term Cap Gain Reinvest", AccountType.TAXABLE.value, CanonicalAction.CAPITAL_GAIN_ST.value),
        ("Cash Dividend", AccountType.TAXABLE.value, CanonicalAction.DIVIDEND_ORDINARY.value),
        ("Special Dividend", AccountType.TAXABLE.value, CanonicalAction.DIVIDEND_ORDINARY.value),
        ("Non-Qualified Div", AccountType.TAXABLE.value, CanonicalAction.DIVIDEND_ORDINARY.value),
        ("Bank Interest", AccountType.TAXABLE.value, CanonicalAction.INTEREST.value),
        ("Bond Interest", AccountType.TAXABLE.value, CanonicalAction.INTEREST.value),
        ("CD Interest", AccountType.TAXABLE.value, CanonicalAction.INTEREST.value),
        ("Credit Interest", AccountType.TAXABLE.value, CanonicalAction.INTEREST.value),
        ("CD Deposit Adj", AccountType.TAXABLE.value, CanonicalAction.CONTRIBUTION.value),
        ("CD Deposit Funds", AccountType.TAXABLE.value, CanonicalAction.CONTRIBUTION.value),
        ("Stock Split", AccountType.TAXABLE.value, CanonicalAction.STOCK_SPLIT.value),
        ("Cash In Lieu", AccountType.TAXABLE.value, CanonicalAction.CASH_IN_LIEU.value),
        ("Long Term Cap Gain", AccountType.TAXABLE.value, CanonicalAction.CAPITAL_GAIN_LT.value),
        ("Short Term Cap Gain", AccountType.TAXABLE.value, CanonicalAction.CAPITAL_GAIN_ST.value),
        ("Internal Transfer", AccountType.TAXABLE.value, CanonicalAction.TRANSFER.value),
        ("Security Transfer", AccountType.TAXABLE.value, CanonicalAction.TRANSFER.value),
        ("MoneyLink Transfer", AccountType.TAXABLE.value, CanonicalAction.TRANSFER.value),
        ("Journal", AccountType.TAXABLE.value, CanonicalAction.JOURNAL.value),
        ("Mystery Action", AccountType.TAXABLE.value, CanonicalAction.OTHER.value),
        # Account-type-conditional Journaled Shares branch
        ("Journaled Shares", AccountType.RSU.value, CanonicalAction.RSU_VEST.value),
        ("Journaled Shares", AccountType.JOINT.value, CanonicalAction.TRANSFER.value),
        ("Journaled Shares", AccountType.TAXABLE.value, CanonicalAction.TRANSFER.value),
    ],
)
def test_map_schwab_action(action: str, account_type: str, expected: str) -> None:
    """REQ-005f/g: every documented Schwab action maps to a canonical action."""
    assert map_schwab_action(action, account_type) == expected


# ── Adapter integration tests ─────────────────────────────────────────────────


def test_adapter_skips_1099_file(
    tmp_path: Path, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """REQ-005f: ``XXXX-X724*.CSV`` files are detected and skipped, no rows inserted."""
    folder = _write_folder(tmp_path, {"XXXX-X724 (1).CSV": SCHWAB_1099_CSV})
    adapter = SchwabCsvAdapter(folder=folder)

    import logging

    caplog.set_level(logging.INFO, logger="src.adapters.schwab_csv")
    result = adapter.run(session)

    assert result.status == IngestionStatus.SUCCESS
    assert result.records_created == 0
    assert session.query(BrokerageTransaction).count() == 0
    assert session.query(PositionSnapshot).count() == 0
    assert session.query(RealizedGainLoss).count() == 0
    # Log mentions Phase 2 deferral.
    assert any("1099 form" in rec.message for rec in caplog.records)


def test_adapter_source_property() -> None:
    """REQ-005g: adapter exposes Source.SCHWAB_CSV value."""
    adapter = SchwabCsvAdapter(folder=Path("."))
    assert adapter.source == Source.SCHWAB_CSV.value


def test_adapter_missing_folder(session: Session) -> None:
    """Missing folder yields FAILURE, no exception."""
    adapter = SchwabCsvAdapter(folder=Path("/nonexistent/schwab/folder"))
    result = adapter.run(session)
    assert result.status == IngestionStatus.FAILURE
    assert len(result.errors) == 1


def test_positions_trailing_rows_filtered(
    tmp_path: Path, session: Session
) -> None:
    """REQ-005c/f: ``Cash & Cash Investments`` and ``Positions Total`` rows skipped."""
    folder = _write_folder(
        tmp_path,
        {"Joint Tenant-Positions-2026-05-03-131725.csv": JOINT_TENANT_POS_CSV},
    )
    adapter = SchwabCsvAdapter(folder=folder)
    result = adapter.run(session)
    assert result.status == IngestionStatus.SUCCESS

    snaps = session.query(PositionSnapshot).all()
    # Two real lots (AMD, GOOG); cash-bucket and Positions Total filtered.
    assert len(snaps) == 2
    symbols = {s.symbol for s in snaps}
    assert symbols == {"AMD", "GOOG"}

    # Account discovered correctly with type=joint.
    acct = session.query(Account).one()
    assert acct.account_name == "Joint Tenant"
    assert acct.account_number == "724"
    assert acct.account_type == AccountType.JOINT.value
    assert acct.broker == Broker.SCHWAB.value


def test_currency_and_quantity_parsing(tmp_path: Path, session: Session) -> None:
    """REQ-005f: ``$``, ``,``, ``-$`` and ``"as of"`` dates parse correctly."""
    folder = _write_folder(
        tmp_path,
        {
            # positions for account-discovery side-effect
            "Joint Tenant-Positions-2026-05-03-131725.csv": JOINT_TENANT_POS_CSV,
            "Joint_Tenant_XXX724_Transactions_20260503-131620.csv": JOINT_TENANT_TX_CSV,
        },
    )
    adapter = SchwabCsvAdapter(folder=folder)
    result = adapter.run(session)
    assert result.status == IngestionStatus.SUCCESS

    txs = session.query(BrokerageTransaction).all()
    by_action = {(t.action, t.symbol): t for t in txs}

    # ``$28.38`` → Decimal("28.38"); ``-$28.38`` → Decimal("-28.38")
    div = by_action[("Reinvest Dividend", "SWVXX")]
    assert div.amount == Decimal("28.38")
    assert div.canonical_action == CanonicalAction.DIVIDEND_ORDINARY.value

    share = by_action[("Reinvest Shares", "SWVXX")]
    assert share.amount == Decimal("-28.38")
    assert share.quantity == Decimal("28.38")
    assert share.price == Decimal("1.00")

    # ``"-1,471"`` quantity comma stripped and normalized to positive (REQ-005b).
    sell = by_action[("Sell", "AMZN")]
    assert sell.quantity == Decimal("1471")
    assert sell.price == Decimal("170.07")
    assert sell.amount == Decimal("250178.09")


def test_as_of_date_split(tmp_path: Path, session: Session) -> None:
    """REQ-005f: ``"02/07/2024 as of 02/05/2024"`` → trade=2024-02-05, settlement=2024-02-07."""
    folder = _write_folder(
        tmp_path,
        {
            "Joint Tenant-Positions-2026-05-03-131725.csv": JOINT_TENANT_POS_CSV,
            "Joint_Tenant_XXX724_Transactions_20260503-131620.csv": JOINT_TENANT_TX_CSV,
        },
    )
    adapter = SchwabCsvAdapter(folder=folder)
    adapter.run(session)

    # The AMZN sell row is the only one with an "as of" date.
    sell = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.symbol == "AMZN")
        .filter(BrokerageTransaction.action == "Sell")
        .one()
    )
    assert sell.trade_date == date(2024, 2, 5)
    assert sell.settlement_date == date(2024, 2, 7)


def test_reinvest_pairing(tmp_path: Path, session: Session) -> None:
    """REQ-005b: ``Reinvest Shares`` paired_transaction_id → preceding ``Reinvest Dividend``."""
    folder = _write_folder(
        tmp_path,
        {
            "Joint Tenant-Positions-2026-05-03-131725.csv": JOINT_TENANT_POS_CSV,
            "Joint_Tenant_XXX724_Transactions_20260503-131620.csv": JOINT_TENANT_TX_CSV,
        },
    )
    adapter = SchwabCsvAdapter(folder=folder)
    adapter.run(session)

    div = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.symbol == "SWVXX")
        .filter(BrokerageTransaction.action == "Reinvest Dividend")
        .one()
    )
    share = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.symbol == "SWVXX")
        .filter(BrokerageTransaction.action == "Reinvest Shares")
        .one()
    )
    assert share.paired_transaction_id == div.id
    # The cash row itself is not back-linked (FK direction is share → cash).
    assert div.paired_transaction_id is None
    # No synthetic rows for Schwab.
    assert all(not t.is_synthetic for t in (div, share))


def test_same_day_duplicate_journaled_shares(
    tmp_path: Path, session: Session
) -> None:
    """REQ-005c/e: same-date AMZN -45 RSU rows persist with distinct hashes via row_index."""
    folder = _write_folder(
        tmp_path,
        {
            "AMZN RSU-Positions-2026-05-03-131716.csv": AMZN_RSU_POS_CSV,
            "AMZN_RSU_XXX144_Transactions_20260503-131654.csv": AMZN_RSU_TX_CSV,
        },
    )
    adapter = SchwabCsvAdapter(folder=folder)
    result = adapter.run(session)
    assert result.status == IngestionStatus.SUCCESS

    journals = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.action == "Journaled Shares")
        .filter(BrokerageTransaction.symbol == "AMZN")
        .filter(BrokerageTransaction.trade_date == date(2025, 12, 11))
        .all()
    )
    # Two identical-data rows persisted (row_index disambiguates the hash).
    assert len(journals) == 2
    assert {j.source_row_hash for j in journals}  # both unique
    assert len({j.source_row_hash for j in journals}) == 2

    # RSU account-type → canonical_action == "rsu_vest" for Journaled Shares.
    # Journaled Shares is not SELL so quantity is stored as-is from source.
    for j in journals:
        assert j.canonical_action == CanonicalAction.RSU_VEST.value
        assert j.quantity == Decimal("-45")  # Journaled Shares not normalized (not SELL)


def test_idempotency_double_ingest(tmp_path: Path, session: Session) -> None:
    """REQ-005e: re-ingesting the same folder produces zero new rows."""
    folder = _write_folder(
        tmp_path,
        {
            "Joint Tenant-Positions-2026-05-03-131725.csv": JOINT_TENANT_POS_CSV,
            "Joint_Tenant_XXX724_Transactions_20260503-131620.csv": JOINT_TENANT_TX_CSV,
            "Joint_Tenant_GainLoss_Realized_Details_20260503-131819.csv": JOINT_TENANT_GL_CSV,
        },
    )

    adapter1 = SchwabCsvAdapter(folder=folder)
    r1 = adapter1.run(session)
    tx_count_after_first = session.query(BrokerageTransaction).count()
    pos_count_after_first = session.query(PositionSnapshot).count()
    gl_count_after_first = session.query(RealizedGainLoss).count()
    assert tx_count_after_first > 0
    assert pos_count_after_first > 0
    assert gl_count_after_first > 0
    assert r1.records_created > 0

    adapter2 = SchwabCsvAdapter(folder=folder)
    r2 = adapter2.run(session)
    assert r2.records_created == 0
    assert r2.records_skipped > 0
    assert session.query(BrokerageTransaction).count() == tx_count_after_first
    assert session.query(PositionSnapshot).count() == pos_count_after_first
    assert session.query(RealizedGainLoss).count() == gl_count_after_first


def test_realized_gainloss_wash_sale_and_term(
    tmp_path: Path, session: Session
) -> None:
    """REQ-005d: ``Wash Sale?`` ``Yes``/``No`` → bool; ``Term`` → GainLossTerm enum."""
    folder = _write_folder(
        tmp_path,
        {
            "Joint Tenant-Positions-2026-05-03-131725.csv": JOINT_TENANT_POS_CSV,
            "Joint_Tenant_GainLoss_Realized_Details_20260503-131819.csv": JOINT_TENANT_GL_CSV,
        },
    )
    adapter = SchwabCsvAdapter(folder=folder)
    result = adapter.run(session)
    assert result.status == IngestionStatus.SUCCESS

    lots = session.query(RealizedGainLoss).order_by(RealizedGainLoss.symbol).all()
    assert len(lots) == 2

    by_symbol = {lot.symbol: lot for lot in lots}

    # VUG long-term, no wash sale.
    vug = by_symbol["VUG"]
    assert vug.term == GainLossTerm.LONG.value
    assert vug.wash_sale is False
    assert vug.lt_gain_loss == Decimal("2.12")
    assert vug.st_gain_loss is None
    assert vug.unadjusted_cost_basis == Decimal("0.61")
    assert vug.gain_loss == Decimal("2.12")
    assert vug.proceeds == Decimal("2.73")
    assert vug.cost_basis == Decimal("0.61")

    # DBGI short-term, wash sale with disallowed loss.
    dbgi = by_symbol["DBGI"]
    assert dbgi.term == GainLossTerm.SHORT.value
    assert dbgi.wash_sale is True
    assert dbgi.disallowed_loss == Decimal("-50.00")
    assert dbgi.st_gain_loss == Decimal("399.96")
    assert dbgi.lt_gain_loss is None
    assert dbgi.quantity == Decimal("200")


def test_amzn_rsu_gainloss(tmp_path: Path, session: Session) -> None:
    """P2-007 / REQ-005d: AMZN RSU G/L lot persists correctly with long-term term."""
    folder = _write_folder(
        tmp_path,
        {
            "AMZN RSU-Positions-2026-05-03-131716.csv": AMZN_RSU_POS_CSV,
            "AMZN_RSU_GainLoss_Realized_Details_20260503.csv": AMZN_RSU_GL_CSV,
        },
    )
    adapter = SchwabCsvAdapter(folder=folder)
    result = adapter.run(session)
    assert result.status == IngestionStatus.SUCCESS

    lots = session.query(RealizedGainLoss).all()
    assert len(lots) == 1
    lot = lots[0]
    assert lot.symbol == "AMZN"
    assert lot.term == GainLossTerm.LONG.value
    assert lot.wash_sale is False
    assert lot.quantity == Decimal("45")
    assert lot.proceeds == Decimal("10362.60")
    assert lot.gain_loss == Decimal("10084.05")


def test_qual_div_reinvest_pairing(tmp_path: Path, session: Session) -> None:
    """P2-006 / REQ-005b: Qual Div Reinvest → Reinvest Shares pair linked via
    paired_transaction_id (cash-side precedes share-side)."""
    folder = _write_folder(
        tmp_path,
        {
            "Joint Tenant-Positions-2026-05-03-131725.csv": JOINT_TENANT_POS_CSV,
            "Joint_Tenant_XXX724_Transactions_20260503-131620.csv": QUAL_DIV_REINVEST_TX_CSV,
        },
    )
    adapter = SchwabCsvAdapter(folder=folder)
    result = adapter.run(session)
    assert result.status == IngestionStatus.SUCCESS

    qual = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.action == "Qual Div Reinvest")
        .one()
    )
    shares = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.action == "Reinvest Shares")
        .one()
    )
    # The share side's paired_transaction_id points to the cash side.
    assert shares.paired_transaction_id == qual.id
    assert qual.canonical_action == CanonicalAction.DIVIDEND_QUALIFIED.value
    assert shares.canonical_action == CanonicalAction.REINVEST.value


def test_no_unintended_other_canonical_action(
    tmp_path: Path, session: Session
) -> None:
    """P1-010 / REQ-005f: every action in the fixture has a known mapping — none
    should fall through to OTHER.  The fixtures cover all Schwab actions used in
    the test data set."""
    folder = _write_folder(
        tmp_path,
        {
            "Joint Tenant-Positions-2026-05-03-131725.csv": JOINT_TENANT_POS_CSV,
            "Joint_Tenant_XXX724_Transactions_20260503-131620.csv": JOINT_TENANT_TX_CSV,
            "AMZN RSU-Positions-2026-05-03-131716.csv": AMZN_RSU_POS_CSV,
            "AMZN_RSU_XXX144_Transactions_20260503-131654.csv": AMZN_RSU_TX_CSV,
        },
    )
    adapter = SchwabCsvAdapter(folder=folder)
    adapter.run(session)

    canonical_actions = {
        t.canonical_action for t in session.query(BrokerageTransaction).all()
    }
    # The fixtures use: Reinvest Dividend → dividend_ordinary, Reinvest Shares → reinvest,
    # Sell → sell, Bank Interest → interest, Journaled Shares (RSU) → rsu_vest,
    # Stock Split → stock_split. None should map to OTHER.
    assert CanonicalAction.OTHER.value not in canonical_actions, (
        f"Unexpected OTHER rows. All canonical actions: {canonical_actions}"
    )


def test_ingestion_log_written(tmp_path: Path, session: Session) -> None:
    """P1-011 / REQ-005g: SchwabCsvAdapter writes exactly one IngestionLog row per run."""
    from src.models.ingestion_log import IngestionLog

    folder = _write_folder(
        tmp_path,
        {
            "Joint Tenant-Positions-2026-05-03-131725.csv": JOINT_TENANT_POS_CSV,
            "Joint_Tenant_XXX724_Transactions_20260503-131620.csv": JOINT_TENANT_TX_CSV,
        },
    )
    adapter = SchwabCsvAdapter(folder=folder)
    result = adapter.run(session)

    logs = (
        session.query(IngestionLog)
        .filter(IngestionLog.source == Source.SCHWAB_CSV.value)
        .all()
    )
    assert len(logs) == 1
    log = logs[0]
    assert log.records_processed == result.records_processed
    assert log.records_failed == result.records_failed
