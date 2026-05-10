"""Tests for brokerage ORM models. REQ-005a..d."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
    BrokerageTxStatus,
    CanonicalAction,
    Entity,
    GainLossTerm,
)


@pytest.fixture
def session() -> Session:
    """Fresh in-memory SQLite with all tables."""
    engine = create_engine("sqlite:///:memory:")
    # Enable FK enforcement on SQLite
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _make_account(session: Session, **overrides: object) -> Account:
    defaults = dict(
        broker=Broker.FIDELITY.value,
        account_number="Z23257759",
        account_name="Individual - TOD",
        account_type=AccountType.TOD.value,
        entity=Entity.PERSONAL.value,
        tax_sheltered=False,
    )
    defaults.update(overrides)
    acct = Account(**defaults)  # type: ignore[arg-type]
    session.add(acct)
    session.flush()
    return acct


# REQ-005a — Account registry --------------------------------------------------


def test_account_unique_broker_account_number(session: Session) -> None:
    """REQ-005a: UNIQUE (broker, account_number) prevents duplicates."""
    _make_account(session)
    session.commit()
    with pytest.raises(IntegrityError):
        _make_account(session, account_name="Dup")  # same broker + number; raises on flush


def test_account_check_constraint_rejects_bad_broker(session: Session) -> None:
    """REQ-005a: CHECK on broker rejects values outside the enum."""
    acct = Account(
        broker="robinhood",  # not in Broker enum
        account_number="X",
        account_type=AccountType.TAXABLE.value,
        entity=Entity.PERSONAL.value,
    )
    session.add(acct)
    with pytest.raises(IntegrityError):
        session.commit()


def test_account_check_constraint_rejects_bad_account_type(session: Session) -> None:
    """REQ-005a: CHECK on account_type rejects bad values (e.g. enum NAME instead of VALUE)."""
    acct = Account(
        broker=Broker.FIDELITY.value,
        account_number="X",
        account_type="K401",  # member name, NOT the value (which is "401k")
        entity=Entity.PERSONAL.value,
    )
    session.add(acct)
    with pytest.raises(IntegrityError):
        session.commit()


def test_account_accepts_401k_value_not_member_name(session: Session) -> None:
    """REQ-005a: account_type='401k' (value) is accepted; 'K401' (name) is not."""
    acct = Account(
        broker=Broker.FIDELITY.value,
        account_number="89766",
        account_type="401k",  # the value, this should work
        entity=Entity.PERSONAL.value,
        is_plan_wrapper=True,
    )
    session.add(acct)
    session.commit()
    assert acct.id is not None


def test_account_parent_child_self_fk(session: Session) -> None:
    """REQ-005a: parent_account_id self-FK supports plan-wrapper relationship."""
    parent = _make_account(
        session,
        account_number="89766",
        account_name="MICROSOFT 401K PLAN",
        account_type=AccountType.K401.value,
        is_plan_wrapper=True,
        tax_sheltered=True,
    )
    child = _make_account(
        session,
        account_number="653373015",
        account_name="BrokerageLink",
        account_type=AccountType.BROKERAGELINK.value,
        parent_account_id=parent.id,
        tax_sheltered=True,
    )
    session.commit()
    assert child.parent is not None
    assert child.parent.account_number == "89766"
    assert parent.children[0].account_number == "653373015"  # type: ignore[attr-defined]


def test_account_beneficiary_field(session: Session) -> None:
    """REQ-005a: 529 accounts store beneficiary."""
    acct = _make_account(
        session,
        broker=Broker.VANGUARD.value,
        account_number="208182839-01",
        account_type=AccountType.K529.value,
        beneficiary="Aiden",
        tax_sheltered=True,
    )
    session.commit()
    assert acct.beneficiary == "Aiden"


# REQ-005b — Brokerage transaction --------------------------------------------


def test_brokerage_transaction_dedup_unique(session: Session) -> None:
    """REQ-005b/e: UNIQUE (account_id, source_row_hash)."""
    acct = _make_account(session)
    session.commit()

    tx1 = BrokerageTransaction(
        account_id=acct.id,
        trade_date=date(2026, 3, 31),
        action="DIVIDEND RECEIVED",
        canonical_action=CanonicalAction.DIVIDEND_ORDINARY.value,
        symbol="VOO",
        amount=Decimal("129.13"),
        source_file="Accounts_History.csv",
        source_row_hash="abc123",
        raw_data={"row": 1},
    )
    session.add(tx1)
    session.commit()

    tx2 = BrokerageTransaction(
        account_id=acct.id,
        trade_date=date(2026, 3, 31),
        action="DIVIDEND RECEIVED",
        canonical_action=CanonicalAction.DIVIDEND_ORDINARY.value,
        symbol="VOO",
        amount=Decimal("129.13"),
        source_file="Accounts_History.csv",
        source_row_hash="abc123",  # same hash
        raw_data={"row": 1},
    )
    session.add(tx2)
    with pytest.raises(IntegrityError):
        session.commit()


def test_brokerage_transaction_paired_link(session: Session) -> None:
    """REQ-005b: paired_transaction_id links dividend ↔ reinvest."""
    acct = _make_account(session)
    session.commit()

    div = BrokerageTransaction(
        account_id=acct.id,
        trade_date=date(2026, 3, 31),
        action="DIVIDEND RECEIVED",
        canonical_action=CanonicalAction.DIVIDEND_ORDINARY.value,
        symbol="VOO",
        amount=Decimal("129.13"),
        source_file="x.csv",
        source_row_hash="div_h",
        raw_data={},
    )
    session.add(div)
    session.flush()

    reinvest = BrokerageTransaction(
        account_id=acct.id,
        trade_date=date(2026, 3, 31),
        action="REINVESTMENT",
        canonical_action=CanonicalAction.REINVEST.value,
        symbol="VOO",
        amount=Decimal("-129.13"),
        quantity=Decimal("0.221"),
        paired_transaction_id=div.id,
        source_file="x.csv",
        source_row_hash="rein_h",
        raw_data={},
    )
    session.add(reinvest)
    session.commit()

    assert reinvest.paired is not None
    assert reinvest.paired.id == div.id


def test_brokerage_transaction_is_synthetic_default_false(session: Session) -> None:
    """REQ-005b: is_synthetic defaults to False."""
    acct = _make_account(session)
    session.commit()
    tx = BrokerageTransaction(
        account_id=acct.id,
        trade_date=date(2026, 1, 1),
        action="Bought",
        canonical_action=CanonicalAction.BUY.value,
        amount=Decimal("-100.00"),
        source_file="x.csv",
        source_row_hash="h1",
        raw_data={},
    )
    session.add(tx)
    session.commit()
    assert tx.is_synthetic is False


def test_brokerage_transaction_canonical_action_check(session: Session) -> None:
    """REQ-005b: CHECK rejects unknown canonical_action values."""
    acct = _make_account(session)
    session.commit()
    tx = BrokerageTransaction(
        account_id=acct.id,
        trade_date=date(2026, 1, 1),
        action="x",
        canonical_action="not_a_real_action",
        amount=Decimal("0"),
        source_file="x.csv",
        source_row_hash="h",
        raw_data={},
    )
    session.add(tx)
    with pytest.raises(IntegrityError):
        session.commit()


def test_brokerage_transaction_decimal_precision(session: Session) -> None:
    """REQ-005b: quantity stores 8 decimal places without loss."""
    acct = _make_account(session)
    session.commit()
    qty = Decimal("0.22134567")
    tx = BrokerageTransaction(
        account_id=acct.id,
        trade_date=date(2026, 1, 1),
        action="REINVESTMENT",
        canonical_action=CanonicalAction.REINVEST.value,
        symbol="VOO",
        quantity=qty,
        amount=Decimal("-129.13"),
        source_file="x.csv",
        source_row_hash="h",
        raw_data={},
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    assert tx.quantity == qty


# REQ-005c — Position snapshot ------------------------------------------------


def test_position_snapshot_same_symbol_two_buckets(session: Session) -> None:
    """REQ-005c: Same symbol can appear twice (different bucket hashes)."""
    acct = _make_account(session, broker=Broker.VANGUARD.value, account_number="65344815")
    session.commit()
    ps1 = PositionSnapshot(
        account_id=acct.id,
        as_of=datetime(2026, 5, 4, 12, 0, 0),
        symbol="VMFXX",
        quantity=Decimal("14873.89"),
        market_value=Decimal("14873.89"),
        source_file="OfxDownload.csv",
        source_row_hash="bucket_a",
        raw_data={},
    )
    ps2 = PositionSnapshot(
        account_id=acct.id,
        as_of=datetime(2026, 5, 4, 12, 0, 0),
        symbol="VMFXX",
        quantity=Decimal("240.03"),
        market_value=Decimal("240.03"),
        source_file="OfxDownload.csv",
        source_row_hash="bucket_b",
        raw_data={},
    )
    session.add_all([ps1, ps2])
    session.commit()
    rows = session.query(PositionSnapshot).filter_by(symbol="VMFXX").all()
    assert len(rows) == 2


def test_position_snapshot_dedup_blocks_same_hash(session: Session) -> None:
    """REQ-005c/e: UNIQUE (account_id, source_row_hash) blocks duplicates."""
    acct = _make_account(session)
    session.commit()
    ps1 = PositionSnapshot(
        account_id=acct.id,
        as_of=datetime(2026, 5, 4),
        symbol="MSFT",
        market_value=Decimal("100"),
        source_file="x.csv",
        source_row_hash="h1",
        raw_data={},
    )
    session.add(ps1)
    session.commit()
    ps2 = PositionSnapshot(
        account_id=acct.id,
        as_of=datetime(2026, 5, 4),
        symbol="MSFT",
        market_value=Decimal("100"),
        source_file="x.csv",
        source_row_hash="h1",
        raw_data={},
    )
    session.add(ps2)
    with pytest.raises(IntegrityError):
        session.commit()


# REQ-005d — Realized gain/loss -----------------------------------------------


def test_realized_gain_loss_term_check(session: Session) -> None:
    """REQ-005d: CHECK constraint rejects bad term values."""
    acct = _make_account(session)
    session.commit()
    rgl = RealizedGainLoss(
        account_id=acct.id,
        symbol="AMZN",
        opened_date=date(2013, 11, 15),
        closed_date=date(2024, 12, 4),
        quantity=Decimal("200"),
        proceeds=Decimal("43616.06"),
        cost_basis=Decimal("3676.31"),
        gain_loss=Decimal("39939.75"),
        term="medium",  # invalid
        source_file="x.csv",
        source_row_hash="h",
        raw_data={},
    )
    session.add(rgl)
    with pytest.raises(IntegrityError):
        session.commit()


def test_realized_gain_loss_long_term_lot(session: Session) -> None:
    """REQ-005d: long-term lot stores LT/ST split, wash-sale flag, unadjusted basis."""
    acct = _make_account(session, broker=Broker.SCHWAB.value, account_number="X724")
    session.commit()
    rgl = RealizedGainLoss(
        account_id=acct.id,
        symbol="AMZN",
        description="AMAZON.COM INC",
        opened_date=date(2013, 11, 15),
        closed_date=date(2024, 12, 4),
        quantity=Decimal("200"),
        proceeds=Decimal("43616.06"),
        cost_basis=Decimal("3676.31"),
        unadjusted_cost_basis=Decimal("3676.31"),
        gain_loss=Decimal("39939.75"),
        lt_gain_loss=Decimal("39939.75"),
        st_gain_loss=Decimal("0"),
        term=GainLossTerm.LONG.value,
        wash_sale=False,
        source_file="GainLoss.csv",
        source_row_hash="h",
        raw_data={},
    )
    session.add(rgl)
    session.commit()
    session.refresh(rgl)
    assert rgl.term == "long"
    assert rgl.gain_loss == Decimal("39939.75")
    assert rgl.wash_sale is False


# P1-013 — BrokerageTxStatus CHECK constraint ---------------------------------


def test_brokerage_tx_status_accepted(session: Session) -> None:
    """P1-013: status='imported' is a valid BrokerageTxStatus value."""
    acct = _make_account(session)
    session.commit()
    tx = BrokerageTransaction(
        account_id=acct.id,
        trade_date=date(2026, 1, 1),
        action="x",
        canonical_action=CanonicalAction.OTHER.value,
        amount=Decimal("0"),
        status=BrokerageTxStatus.IMPORTED.value,  # "imported"
        source_file="x.csv",
        source_row_hash="status_ok",
        raw_data={},
    )
    session.add(tx)
    session.commit()  # should not raise
    assert tx.status == "imported"


def test_brokerage_tx_status_rejects_bad_value(session: Session) -> None:
    """P1-013: status='pending' is NOT a valid BrokerageTxStatus value → IntegrityError."""
    acct = _make_account(session)
    session.commit()
    tx = BrokerageTransaction(
        account_id=acct.id,
        trade_date=date(2026, 1, 1),
        action="x",
        canonical_action=CanonicalAction.OTHER.value,
        amount=Decimal("0"),
        status="pending",  # invalid — not in the CHECK constraint
        source_file="x.csv",
        source_row_hash="status_bad",
        raw_data={},
    )
    session.add(tx)
    with pytest.raises(IntegrityError):
        session.commit()


# REQ-005a — cascade delete ---------------------------------------------------


def test_account_delete_cascades(session: Session) -> None:
    """P1-014 / REQ-005a: Deleting an account removes its transactions, positions,
    and realized G/L rows."""
    acct = _make_account(session)
    session.commit()
    session.add(
        BrokerageTransaction(
            account_id=acct.id,
            trade_date=date(2026, 1, 1),
            action="x",
            canonical_action=CanonicalAction.OTHER.value,
            amount=Decimal("0"),
            source_file="x.csv",
            source_row_hash="h_tx",
            raw_data={},
        )
    )
    # P1-014: also add a PositionSnapshot and a RealizedGainLoss.
    session.add(
        PositionSnapshot(
            account_id=acct.id,
            as_of=datetime(2026, 5, 4),
            symbol="MSFT",
            market_value=Decimal("100"),
            source_file="x.csv",
            source_row_hash="h_pos",
            raw_data={},
        )
    )
    session.add(
        RealizedGainLoss(
            account_id=acct.id,
            symbol="AMZN",
            closed_date=date(2026, 1, 1),
            quantity=Decimal("1"),
            proceeds=Decimal("100"),
            cost_basis=Decimal("80"),
            gain_loss=Decimal("20"),
            source_file="x.csv",
            source_row_hash="h_gl",
            raw_data={},
        )
    )
    session.commit()

    assert session.query(BrokerageTransaction).count() == 1
    assert session.query(PositionSnapshot).count() == 1
    assert session.query(RealizedGainLoss).count() == 1

    session.delete(acct)
    session.commit()

    assert session.query(BrokerageTransaction).count() == 0
    assert session.query(PositionSnapshot).count() == 0
    assert session.query(RealizedGainLoss).count() == 0
