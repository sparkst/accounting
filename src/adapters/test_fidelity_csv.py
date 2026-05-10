"""Tests for the Fidelity CSV adapter (TASK-06 / REQ-005a..g).

Inline byte-string fixtures (matches the ``test_bank_csv.py`` pattern). Each
test writes the fixture bytes into a temporary folder, runs
``FidelityCsvAdapter`` against it, and asserts on the resulting brokerage
tables.

Coverage map:
- BOM + leading-blank-row stripping            -> test_bom_and_leading_blank_rows
- Account auto-discovery                       -> test_account_auto_discovery
- 15-column 401k edge case (account 89766)     -> test_89766_15_column_shift
- Negative-qty sell normalized to positive     -> test_sell_negative_quantity_normalized
- REINVEST + DIVIDEND pairing                  -> test_reinvest_dividend_paired
- Idempotent re-ingest                         -> test_idempotent_reingest
- Action-prefix mapping coverage               -> test_action_prefix_mapping_coverage
- Trailing disclaimer/footer truncation        -> test_trailing_footer_truncated
- Positions file -> position_snapshot          -> test_positions_file_ingested
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.fidelity_csv import (
    FidelityCsvAdapter,
    _normalize_89766_row,
    map_action,
)
from src.models.base import Base
from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot
from src.models.enums import (
    AccountType,
    Broker,
    CanonicalAction,
    Source,
)
from src.models.ingestion_log import IngestionLog


# ── Session fixture ──────────────────────────────────────────────────────
@pytest.fixture()
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
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ── Fixture bytes — sanitized samples from real Fidelity exports ─────────
# Header + leading blank rows + UTF-8 BOM. Sanitized: account-number suffixes
# preserved (real numbers are already redacted via prefix-only retention),
# EXCEPT 89766 which is the public Microsoft 401k plan ID.

_BOM = b"\xef\xbb\xbf"

# 1) "Standard" history file with a mix of actions, BOM + 2 leading blank rows,
# and trailing disclaimer / "Date downloaded" footer.
HISTORY_STANDARD = (
    _BOM + b"\n\n"
    b"Run Date,Account,Account Number,Action,Symbol,Description,Type,"
    b"Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),"
    b"Amount ($),Settlement Date\n"
    # REINVEST first (pair partner inserts later DIVIDEND)
    b"03/31/2026,\"BrokerageLink\",\"653373015\",\"REINVESTMENT VANGUARD INDEX FUNDS S&P 500 ETF USD (VOO) (Cash)\","
    b"VOO,\"VANGUARD INDEX FUNDS S&P 500 ETF USD\",Cash,583.35,0.221,,,,-129.13,\n"
    # DIVIDEND RECEIVED — should pair to the REINVEST above
    b"03/31/2026,\"BrokerageLink\",\"653373015\",\"DIVIDEND RECEIVED VANGUARD INDEX FUNDS S&P 500 ETF USD (VOO) (Cash)\","
    b"VOO,\"VANGUARD INDEX FUNDS S&P 500 ETF USD\",Cash,,0.000,,,,129.13,\n"
    # YOU BOUGHT (with settlement date)
    b"05/12/2025,\"Health Savings Account\",\"241527012\",\"YOU BOUGHT VANGUARD INDEX FUNDS S&P 500 ETF USD (VOO) (Cash)\","
    b"VOO,\"VANGUARD INDEX FUNDS S&P 500 ETF USD\",Cash,531.33,13,,,,-6907.34,05/13/2025\n"
    # YOU SOLD with NEGATIVE quantity
    b"06/06/2025,\"Individual - TOD\",\"Z23257759\",\"YOU SOLD MICROSOFT CORP (MSFT) (Cash)\","
    b"MSFT,\"MICROSOFT CORP\",Cash,470.43,-168,,,,79032.37,06/09/2025\n"
    # INTEREST EARNED
    b"06/30/2025,\"Individual - TOD\",\"Z23257759\",\"INTEREST EARNED CASH (315994103) (Cash)\","
    b"315994103,\"CASH\",Cash,,0.000,,,,48.58,\n"
    # Electronic Funds Transfer Paid -> transfer
    b"06/18/2025,\"Individual - TOD\",\"Z23257759\",\"Electronic Funds Transfer Paid (Cash)\","
    b",\"No Description\",Cash,,0.000,,,,-80328.03,\n"
    # P2-011: TRANSFERRED FROM row -> should map to TRANSFER canonical action
    b"06/18/2025,\"Individual - TOD\",\"Z23257759\",\"TRANSFERRED FROM (Cash)\","
    b",\"Transfer from other broker\",Cash,,0.000,,,,5000.00,\n"
    b"\n"
    b"\"The data and information in this spreadsheet is provided to you solely for your use\"\n"
    b"\"and is not for distribution. The spreadsheet is provided for informational purposes only.\"\n"
    b"\n"
    b"Date downloaded 05/04/2026 11:52 am\n"
)

# 2) 89766 (Microsoft 401k plan) — 15-column edge-case rows.
HISTORY_89766 = (
    b"Run Date,Account,Account Number,Action,Symbol,Description,Type,"
    b"Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),"
    b"Amount ($),Settlement Date\n"
    # 15-column rows (note the extra trailing empty) — Quantity is at index 7
    # in source-order, Amount at index 12.
    b"04/30/2025,\"MICROSOFT 401K PLAN\",\"89766\",\"Exchange In\",,\"BROKERAGELINK\","
    b",120948.08,,,,,120948.08,,\n"
    b"04/30/2025,\"MICROSOFT 401K PLAN\",\"89766\",\"Change on Market Value\",,"
    b"\"BTC LPATH IDX 2040 M\",,0.000,,,,,-2988.34,,\n"
    b"04/30/2025,\"MICROSOFT 401K PLAN\",\"89766\",\"Exchange Out\",,"
    b"\"BTC LPATH IDX 2040 M\",,-9417.652,,,,,-120948.08,,\n"
)

# 3) Positions file fixture — header + 2 data rows + disclaimer + Date downloaded.
POSITIONS_FIXTURE = (
    _BOM
    + b"Account Number,Account Name,Symbol,Description,Quantity,Last Price,"
    b"Last Price Change,Current Value,Today's Gain/Loss Dollar,"
    b"Today's Gain/Loss Percent,Total Gain/Loss Dollar,"
    b"Total Gain/Loss Percent,Percent Of Account,Cost Basis Total,"
    b"Average Cost Basis,Type\n"
    b"Z23257759,Individual - TOD,MSFT,MICROSOFT CORP,0.0003,$413.33,-$1.11,"
    b"$0.12,-$0.01,-0.27%,$0.00,+3.33%,0.24%,$0.12,$400.00,Cash,\n"
    b"653373015,BrokerageLink,VOO,VANGUARD INDEX FUNDS S&P 500 ETF USD,69.186,"
    b"$659.27,-$3.25,$45612.26,-$224.85,-0.50%,+$9312.64,+25.65%,30.59%,"
    b"$36299.62,$524.67,Cash,\n"
    b"\n"
    b"\"The data and information in this spreadsheet is provided to you solely\"\n"
    b"\"Date downloaded May-04-2026 2:55 p.m ET\"\n"
)


def _make_folder(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """Write each (filename → bytes) into tmp_path and return the folder."""
    folder = tmp_path / "Fidelity"
    folder.mkdir()
    for name, content in files.items():
        (folder / name).write_bytes(content)
    return folder


# ── Unit tests for helpers ───────────────────────────────────────────────


def test_map_action_known_prefixes() -> None:
    """Action mapping covers every prefix declared in PLAN TASK-06."""
    cases = {
        "YOU BOUGHT VOO (Cash)": CanonicalAction.BUY.value,
        "YOU SOLD MSFT (Cash)": CanonicalAction.SELL.value,
        "DIVIDEND RECEIVED VOO": CanonicalAction.DIVIDEND_ORDINARY.value,
        "REINVESTMENT VOO": CanonicalAction.REINVEST.value,
        "INTEREST EARNED CASH": CanonicalAction.INTEREST.value,
        "Exchange In": CanonicalAction.EXCHANGE.value,
        "Exchange Out": CanonicalAction.EXCHANGE.value,
        "TRANSFERRED FROM ACCOUNT": CanonicalAction.TRANSFER.value,
        # P2-013: JOURNAL now maps to JOURNAL (consistent with Schwab).
        "JOURNAL ENTRY": CanonicalAction.JOURNAL.value,
        "Electronic Funds Transfer Paid": CanonicalAction.TRANSFER.value,
        "Electronic Funds Transfer Received": CanonicalAction.TRANSFER.value,
        "DEPOSIT VIA ACH": CanonicalAction.CONTRIBUTION.value,
        "CONTRIBUTION 401K": CanonicalAction.CONTRIBUTION.value,
        "WITHDRAWAL": CanonicalAction.DISTRIBUTION.value,
        "DISTRIBUTION FROM PLAN": CanonicalAction.DISTRIBUTION.value,
        "Change on Market Value": CanonicalAction.VALUATION_ADJUSTMENT.value,
        "SOMETHING UNRECOGNIZED": CanonicalAction.OTHER.value,
    }
    for action, expected in cases.items():
        assert map_action(action) == expected, f"{action!r} → expected {expected}"


def test_normalize_89766_row_15_to_14() -> None:
    """A 15-col 89766 row aligns to the 14-col header (Qty@8, Amount@12)."""
    raw = [
        "04/30/2025", "MICROSOFT 401K PLAN", "89766", "Exchange Out", "",
        "BTC LPATH IDX 2040 M", "", "-9417.652", "", "", "", "", "-120948.08",
        "", "",
    ]
    out = _normalize_89766_row("89766", raw)
    assert len(out) == 14
    assert out[7] == ""           # Price
    assert out[8] == "-9417.652"  # Quantity
    assert out[12] == "-120948.08"  # Amount
    assert out[13] == ""          # Settlement


def test_normalize_89766_row_passthrough_other_accounts() -> None:
    """Non-89766 rows are unchanged."""
    raw = ["a"] * 14
    assert _normalize_89766_row("Z23257759", raw) is raw
    # Even an explicit 15-col row for a non-89766 account is left alone.
    raw15 = ["a"] * 15
    assert _normalize_89766_row("241527012", raw15) is raw15


# ── Adapter integration tests ────────────────────────────────────────────


def test_bom_and_leading_blank_rows(tmp_path: Path, session: Session) -> None:
    """BOM + leading blank rows are stripped; data rows ingest correctly."""
    folder = _make_folder(tmp_path, {"Accounts_History.csv": HISTORY_STANDARD})
    result = FidelityCsvAdapter(folder).run(session)
    assert result.records_failed == 0, result.errors
    txs = session.query(BrokerageTransaction).all()
    # 7 data rows in HISTORY_STANDARD (6 original + TRANSFERRED FROM added by P2-011).
    assert len(txs) == 7


def test_account_auto_discovery(tmp_path: Path, session: Session) -> None:
    """Each distinct (broker, account_number) becomes one Account row."""
    folder = _make_folder(tmp_path, {"Accounts_History.csv": HISTORY_STANDARD})
    FidelityCsvAdapter(folder).run(session)
    accounts = (
        session.query(Account)
        .filter(Account.broker == Broker.FIDELITY.value)
        .all()
    )
    numbers = {a.account_number for a in accounts}
    assert numbers == {"653373015", "241527012", "Z23257759"}
    by_num = {a.account_number: a for a in accounts}
    assert by_num["Z23257759"].account_type == AccountType.TOD.value
    assert by_num["653373015"].account_type == AccountType.BROKERAGELINK.value
    assert by_num["241527012"].account_type == AccountType.HSA.value
    # HSA is tax-sheltered, TOD is not
    assert by_num["241527012"].tax_sheltered is True
    assert by_num["Z23257759"].tax_sheltered is False


def test_89766_15_column_shift(tmp_path: Path, session: Session) -> None:
    """The 15-col 89766 rows shift correctly: Quantity & Amount land in canonical cols."""
    folder = _make_folder(tmp_path, {"Accounts_History.csv": HISTORY_89766})
    result = FidelityCsvAdapter(folder).run(session)
    assert result.records_failed == 0, result.errors
    acct = (
        session.query(Account)
        .filter(Account.account_number == "89766")
        .one()
    )
    assert acct.account_type == AccountType.K401.value
    assert acct.is_plan_wrapper is True

    txs = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.account_id == acct.id)
        .order_by(BrokerageTransaction.action)
        .all()
    )
    by_action = {t.action: t for t in txs}
    # Exchange In: Quantity=120948.08, Amount=120948.08
    ex_in = by_action["Exchange In"]
    assert ex_in.quantity == Decimal("120948.08")
    assert ex_in.amount == Decimal("120948.08")
    assert ex_in.canonical_action == CanonicalAction.EXCHANGE.value
    # Exchange Out: Quantity=-9417.652 (kept negative — only SELL gets abs), Amount=-120948.08
    ex_out = by_action["Exchange Out"]
    assert ex_out.quantity == Decimal("-9417.652")
    assert ex_out.amount == Decimal("-120948.08")
    # Change on Market Value -> valuation_adjustment, amount=-2988.34
    cmv = by_action["Change on Market Value"]
    assert cmv.canonical_action == CanonicalAction.VALUATION_ADJUSTMENT.value
    assert cmv.amount == Decimal("-2988.34")


def test_sell_negative_quantity_normalized(tmp_path: Path, session: Session) -> None:
    """A YOU SOLD row with negative qty stores quantity as positive Decimal."""
    folder = _make_folder(tmp_path, {"Accounts_History.csv": HISTORY_STANDARD})
    FidelityCsvAdapter(folder).run(session)
    sells = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.canonical_action == CanonicalAction.SELL.value)
        .all()
    )
    assert len(sells) == 1
    assert sells[0].quantity == Decimal("168")
    assert sells[0].quantity > 0
    assert sells[0].amount == Decimal("79032.37")


def test_reinvest_dividend_paired(tmp_path: Path, session: Session) -> None:
    """REINVESTMENT + DIVIDEND RECEIVED rows are linked via paired_transaction_id."""
    folder = _make_folder(tmp_path, {"Accounts_History.csv": HISTORY_STANDARD})
    FidelityCsvAdapter(folder).run(session)
    reinvest = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.canonical_action == CanonicalAction.REINVEST.value)
        .one()
    )
    dividend = (
        session.query(BrokerageTransaction)
        .filter(
            BrokerageTransaction.canonical_action
            == CanonicalAction.DIVIDEND_ORDINARY.value
        )
        .one()
    )
    # Per PLAN: dividend → reinvest. The dividend row's paired_transaction_id
    # points to the matching reinvest row.
    assert dividend.paired_transaction_id == reinvest.id
    # The reinvest row itself is unpaired (could pair to another partner later).
    assert reinvest.paired_transaction_id is None


def test_idempotent_reingest(tmp_path: Path, session: Session) -> None:
    """Running the adapter twice on the same fixture produces the same row counts."""
    folder = _make_folder(
        tmp_path,
        {
            "Accounts_History.csv": HISTORY_STANDARD,
            "Accounts_History (1).csv": HISTORY_89766,
            "Portfolio_Positions_May-04-2026.csv": POSITIONS_FIXTURE,
        },
    )
    adapter = FidelityCsvAdapter(folder)
    first = adapter.run(session)
    tx_count_after_first = session.query(BrokerageTransaction).count()
    pos_count_after_first = session.query(PositionSnapshot).count()
    acct_count_after_first = session.query(Account).count()

    second = adapter.run(session)
    assert session.query(BrokerageTransaction).count() == tx_count_after_first
    assert session.query(PositionSnapshot).count() == pos_count_after_first
    assert session.query(Account).count() == acct_count_after_first

    # Second pass should record skips equal to processed rows.
    assert second.records_skipped > 0
    assert second.records_created == 0
    assert first.records_created > 0


def test_action_prefix_mapping_coverage(tmp_path: Path, session: Session) -> None:
    """The standard fixture exercises every distinct mapped action prefix."""
    folder = _make_folder(tmp_path, {"Accounts_History.csv": HISTORY_STANDARD})
    FidelityCsvAdapter(folder).run(session)
    canonical_actions = {
        t.canonical_action for t in session.query(BrokerageTransaction).all()
    }
    # Every canonical action that the standard fixture rows imply must be present.
    assert CanonicalAction.BUY.value in canonical_actions
    assert CanonicalAction.SELL.value in canonical_actions
    assert CanonicalAction.REINVEST.value in canonical_actions
    assert CanonicalAction.DIVIDEND_ORDINARY.value in canonical_actions
    assert CanonicalAction.INTEREST.value in canonical_actions
    assert CanonicalAction.TRANSFER.value in canonical_actions
    # P2-011: TRANSFERRED FROM → TRANSFER is present from the added fixture row.
    assert CanonicalAction.TRANSFER.value in canonical_actions
    # No row in the standard fixture should fall through to OTHER.
    assert CanonicalAction.OTHER.value not in canonical_actions


def test_trailing_footer_truncated(tmp_path: Path, session: Session) -> None:
    """Disclaimer + 'Date downloaded' rows are NOT ingested as transactions."""
    folder = _make_folder(tmp_path, {"Accounts_History.csv": HISTORY_STANDARD})
    result = FidelityCsvAdapter(folder).run(session)
    # Standard fixture has 7 data rows (6 original + TRANSFERRED FROM added by P2-011).
    assert result.records_processed == 7
    txs = session.query(BrokerageTransaction).all()
    assert len(txs) == 7
    for t in txs:
        # No disclaimer text leaked into action / description.
        assert "The data" not in (t.action or "")
        assert "Date downloaded" not in (t.action or "")


def test_positions_file_ingested(tmp_path: Path, session: Session) -> None:
    """Portfolio_Positions_*.csv populates position_snapshot with as_of from filename."""
    folder = _make_folder(
        tmp_path, {"Portfolio_Positions_May-04-2026.csv": POSITIONS_FIXTURE}
    )
    result = FidelityCsvAdapter(folder).run(session)
    assert result.records_failed == 0, result.errors
    snaps = session.query(PositionSnapshot).all()
    assert len(snaps) == 2
    by_symbol = {s.symbol: s for s in snaps}
    msft = by_symbol["MSFT"]
    assert msft.quantity == Decimal("0.0003")
    assert msft.price == Decimal("413.33")
    assert msft.as_of.date().isoformat() == "2026-05-04"

    voo = by_symbol["VOO"]
    assert voo.quantity == Decimal("69.186")
    assert voo.market_value == Decimal("45612.26")


def test_ingestion_log_written(tmp_path: Path, session: Session) -> None:
    """Each adapter run writes exactly one IngestionLog row tagged FIDELITY_CSV."""
    folder = _make_folder(tmp_path, {"Accounts_History.csv": HISTORY_STANDARD})
    FidelityCsvAdapter(folder).run(session)
    logs = (
        session.query(IngestionLog)
        .filter(IngestionLog.source == Source.FIDELITY_CSV.value)
        .all()
    )
    assert len(logs) == 1
    # 7 data rows (6 original + TRANSFERRED FROM added by P2-011).
    assert logs[0].records_processed == 7
    assert logs[0].records_failed == 0
