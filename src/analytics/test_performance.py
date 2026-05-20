"""REQ-PERF-005..009 tests — 10 edge cases from spec §6 + TWR + XIRR fixtures."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import src.models.plaid as _plaid  # noqa: F401  # registers PlaidItem for FK
from src.analytics.classify import PortfolioScope, PositionScope
from src.analytics.performance import (
    CashFlow,
    DailyPoint,
    money_weighted_return,
    principal_growth_series,
    time_weighted_return,
    tracked_value_at,
)
from src.models.base import Base
from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot
from src.models.enums import (
    AccountType,
    Broker,
    BrokerageTxStatus,
    CanonicalAction,
    CashFlowType,
    Entity,
)
from src.models.history import CostBasisLot, HistoricalPrice


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _acct(s: Session, acct_id: str = "acct-1") -> Account:
    acct = Account(
        id=acct_id,
        broker=Broker.SCHWAB.value,
        account_number=f"NUM-{acct_id}",
        account_name=acct_id,
        account_type=AccountType.TAXABLE.value,
        entity=Entity.PERSONAL.value,
    )
    s.add(acct)
    s.commit()
    return acct


def _tx(
    s: Session,
    *,
    acct_id: str,
    action: CanonicalAction,
    amount: Decimal,
    trade_date: date,
    symbol: str | None = None,
    paired_id: str | None = None,
    tx_id: str | None = None,
    cash_flow_type: CashFlowType = CashFlowType.NONE,
) -> BrokerageTransaction:
    the_id = tx_id or str(uuid.uuid4())
    tx = BrokerageTransaction(
        id=the_id,
        account_id=acct_id,
        trade_date=trade_date,
        action=action.value,
        canonical_action=action.value,
        symbol=symbol,
        amount=amount,
        status=BrokerageTxStatus.IMPORTED.value,
        source_file="test.csv",
        source_row_hash=the_id,
        raw_data={},
        paired_transaction_id=paired_id,
        cash_flow_type=cash_flow_type.value,
    )
    s.add(tx)
    s.commit()
    return tx


def _snap(
    s: Session,
    *,
    acct_id: str,
    symbol: str | None,
    as_of: datetime,
    qty: Decimal,
    market_value: Decimal,
    cost_basis: Decimal | None = None,
) -> PositionSnapshot:
    snap = PositionSnapshot(
        id=str(uuid.uuid4()),
        account_id=acct_id,
        symbol=symbol,
        as_of=as_of,
        quantity=qty,
        market_value=market_value,
        cost_basis=cost_basis,
        source_file="test.csv",
        source_row_hash=str(uuid.uuid4()),
        raw_data={},
    )
    s.add(snap)
    s.commit()
    return snap


def _price(s: Session, *, symbol: str, trade_date: date, close: Decimal) -> HistoricalPrice:
    hp = HistoricalPrice(symbol=symbol, trade_date=trade_date, close=close, source="test")
    s.add(hp)
    s.commit()
    return hp


def _lot(
    s: Session,
    *,
    acct_id: str | None,
    symbol: str,
    open_date: date,
    cost_total: Decimal,
) -> CostBasisLot:
    lot = CostBasisLot(
        id=str(uuid.uuid4()),
        account_id=acct_id,
        raw_account_name="test",
        symbol=symbol,
        open_date=open_date,
        quantity=Decimal("1"),
        cost_per_share=cost_total,
        cost_total=cost_total,
        source="test",
        source_row_hash=str(uuid.uuid4()),
    )
    s.add(lot)
    s.commit()
    return lot


# ── Edge-case 1: empty position ───────────────────────────────────────────────


class TestEdgeCase1EmptyPosition:
    def test_xirr_none_on_single_deposit_no_time(self) -> None:
        """Spec §6 #1: empty position with single same-date deposit → XIRR=None."""
        cfs = [CashFlow(date(2025, 1, 15), Decimal("-10000"))]
        assert money_weighted_return(cfs, Decimal("0"), date(2025, 1, 15)) is None

    def test_sold_all_shares_market_value_zero(self, session: Session) -> None:
        """Spec §6 #1: after selling all shares, market_value = 0."""
        _acct(session)
        _snap(session, acct_id="acct-1", symbol="VTI",
              as_of=datetime(2025, 1, 1), qty=Decimal("0"), market_value=Decimal("0"))
        _price(session, symbol="VTI", trade_date=date(2025, 1, 1), close=Decimal("250.00"))
        series = principal_growth_series(
            session, PortfolioScope(), date(2025, 1, 1), date(2025, 1, 1)
        )
        assert len(series) == 1
        assert series[0].market_value == Decimal("0")


# ── Edge-case 2: single deposit no time ───────────────────────────────────────


class TestEdgeCase2SingleDepositNoTime:
    def test_twr_zero_for_no_time(self) -> None:
        """Spec §6 #2: V_begin=10000, V_end=10000, single CF on start day → TWR=0."""
        pts = [
            DailyPoint(
                date=date(2025, 1, 1),
                market_value=Decimal("10000"),
                principal=Decimal("10000"),
                growth=Decimal("0"),
            )
        ]
        cfs = [CashFlow(date(2025, 1, 1), Decimal("-10000"))]
        assert time_weighted_return(pts, cfs, [date(2025, 1, 1)]) == Decimal("0.000000")

    def test_xirr_none_for_no_time(self) -> None:
        """Spec §6 #2: XIRR with single deposit + terminal on same date → None."""
        cfs = [CashFlow(date(2025, 6, 1), Decimal("-5000"))]
        assert money_weighted_return(cfs, Decimal("5000"), date(2025, 6, 1)) is None


# ── Edge-case 3: position opened mid-window ───────────────────────────────────


class TestEdgeCase3PositionOpenedMidWindow:
    def test_principal_zero_before_first_tx(self, session: Session) -> None:
        """Spec §6 #3: no tx before window → principal=0 until the first tx."""
        _acct(session)
        _tx(
            session,
            acct_id="acct-1",
            action=CanonicalAction.CONTRIBUTION,
            amount=Decimal("5000"),
            trade_date=date(2025, 3, 1),
            cash_flow_type=CashFlowType.EXTERNAL_IN,
        )
        _snap(
            session,
            acct_id="acct-1",
            symbol="CASH",
            as_of=datetime(2025, 3, 1),
            qty=Decimal("0"),
            market_value=Decimal("5000"),
        )
        series = principal_growth_series(
            session, PortfolioScope(), date(2025, 1, 1), date(2025, 3, 2)
        )
        jan1 = next(p for p in series if p.date == date(2025, 1, 1))
        mar1 = next(p for p in series if p.date == date(2025, 3, 1))
        assert jan1.principal == Decimal("0")
        assert mar1.principal == Decimal("5000")


# ── Edge-case 4: negative XIRR ────────────────────────────────────────────────


class TestEdgeCase4NegativeXirr:
    def test_negative_xirr_lost_money(self) -> None:
        """Spec §6 #4: invest 10000, get back 9000 in 1y → XIRR ≈ −0.1."""
        cfs = [CashFlow(date(2025, 1, 1), Decimal("-10000"))]
        result = money_weighted_return(cfs, Decimal("9000"), date(2026, 1, 1))
        assert result is not None
        # NPV(-0.1) = -10000 + 9000/0.9 = 0 → exactly -0.1
        assert abs(result - Decimal("-0.1")) < Decimal("0.0001")


# ── Edge-case 5: stock split ──────────────────────────────────────────────────


class TestEdgeCase5StockSplit:
    def test_stock_split_no_principal_change(self, session: Session) -> None:
        """Spec §6 #5: STOCK_SPLIT has CashFlowType.NONE — no principal change."""
        _acct(session)
        _tx(
            session,
            acct_id="acct-1",
            action=CanonicalAction.STOCK_SPLIT,
            amount=Decimal("0"),
            trade_date=date(2025, 6, 1),
            symbol="TSLA",
            cash_flow_type=CashFlowType.NONE,
        )
        _snap(
            session,
            acct_id="acct-1",
            symbol="TSLA",
            as_of=datetime(2025, 6, 1),
            qty=Decimal("200"),
            market_value=Decimal("40000"),
        )
        _price(session, symbol="TSLA", trade_date=date(2025, 6, 1), close=Decimal("200.00"))
        series = principal_growth_series(
            session, PortfolioScope(), date(2025, 6, 1), date(2025, 6, 1)
        )
        assert series[0].principal == Decimal("0")
        assert series[0].market_value == Decimal("40000.00")


# ── Edge-case 6: reinvest dividend scope asymmetry ────────────────────────────


class TestEdgeCase6ReinvestDividendScope:
    def test_reinvest_none_at_portfolio_scope(self, session: Session) -> None:
        """Spec §6 #6: REINVEST is NONE at portfolio scope — no principal change."""
        _acct(session)
        _tx(
            session,
            acct_id="acct-1",
            action=CanonicalAction.REINVEST,
            amount=Decimal("100"),
            trade_date=date(2025, 6, 1),
            symbol="VTI",
            cash_flow_type=CashFlowType.NONE,
        )
        series = principal_growth_series(
            session, PortfolioScope(), date(2025, 6, 1), date(2025, 6, 1)
        )
        assert series[0].principal == Decimal("0")

    def test_reinvest_external_in_at_position_scope(self, session: Session) -> None:
        """Spec §6 #6 (critical asymmetry): REINVEST is external_in at position scope."""
        _acct(session)
        _tx(
            session,
            acct_id="acct-1",
            action=CanonicalAction.REINVEST,
            amount=Decimal("100"),
            trade_date=date(2025, 6, 1),
            symbol="VTI",
            cash_flow_type=CashFlowType.NONE,
        )
        series = principal_growth_series(
            session,
            PositionScope("VTI", "acct-1"),
            date(2025, 6, 1),
            date(2025, 6, 1),
        )
        assert series[0].principal == Decimal("100")


# ── Edge-case 7: internal transfer mid-window ─────────────────────────────────


class TestEdgeCase7InternalTransferMidWindow:
    def test_paired_transfer_no_portfolio_principal_change(self, session: Session) -> None:
        """Spec §6 #7: paired transfer → internal at portfolio scope → principal unchanged."""
        _acct(session, "A1")
        _acct(session, "A2")
        _tx(
            session,
            acct_id="A1",
            action=CanonicalAction.TRANSFER,
            amount=Decimal("5000"),
            trade_date=date(2025, 6, 1),
            tx_id="t1",
            paired_id="t2",
            cash_flow_type=CashFlowType.INTERNAL,
        )
        _tx(
            session,
            acct_id="A2",
            action=CanonicalAction.TRANSFER,
            amount=Decimal("-5000"),
            trade_date=date(2025, 6, 1),
            tx_id="t2",
            paired_id="t1",
            cash_flow_type=CashFlowType.INTERNAL,
        )
        series = principal_growth_series(
            session, PortfolioScope(), date(2025, 6, 1), date(2025, 6, 1)
        )
        assert series[0].principal == Decimal("0")


# ── Edge-case 8: unpaired transfer ────────────────────────────────────────────


class TestEdgeCase8UnpairedTransfer:
    def test_unpaired_transfer_is_external(self, session: Session) -> None:
        """Spec §6 #8: unpaired transfer defaults to external_in/out — principal steps."""
        _acct(session)
        _tx(
            session,
            acct_id="acct-1",
            action=CanonicalAction.TRANSFER,
            amount=Decimal("3000"),
            trade_date=date(2025, 6, 1),
            cash_flow_type=CashFlowType.EXTERNAL_IN,
        )
        series = principal_growth_series(
            session, PortfolioScope(), date(2025, 6, 1), date(2025, 6, 1)
        )
        assert series[0].principal == Decimal("3000")


# ── Edge-case 9: RSU vest ─────────────────────────────────────────────────────


class TestEdgeCase9RsuVest:
    def test_rsu_vest_external_in_at_portfolio(self, session: Session) -> None:
        """Spec §6 #9: RSU_VEST is external_in at portfolio scope (gross FMV)."""
        _acct(session)
        _tx(
            session,
            acct_id="acct-1",
            action=CanonicalAction.RSU_VEST,
            amount=Decimal("12000"),
            trade_date=date(2025, 3, 15),
            symbol="GSK",
            cash_flow_type=CashFlowType.EXTERNAL_IN,
        )
        series = principal_growth_series(
            session, PortfolioScope(), date(2025, 3, 15), date(2025, 3, 15)
        )
        assert series[0].principal == Decimal("12000")


# ── Edge-case 10: window boundary cash flow ───────────────────────────────────


class TestEdgeCase10WindowBoundaryCashFlow:
    def test_flows_on_start_and_end_included(self, session: Session) -> None:
        """Spec §6 #10: cash flows on exact start/end dates ARE included."""
        _acct(session)
        _tx(
            session,
            acct_id="acct-1",
            action=CanonicalAction.CONTRIBUTION,
            amount=Decimal("1000"),
            trade_date=date(2025, 1, 1),
            cash_flow_type=CashFlowType.EXTERNAL_IN,
        )
        _tx(
            session,
            acct_id="acct-1",
            action=CanonicalAction.CONTRIBUTION,
            amount=Decimal("2000"),
            trade_date=date(2025, 3, 31),
            cash_flow_type=CashFlowType.EXTERNAL_IN,
        )
        series = principal_growth_series(
            session, PortfolioScope(), date(2025, 1, 1), date(2025, 3, 31)
        )
        first = next(p for p in series if p.date == date(2025, 1, 1))
        last = next(p for p in series if p.date == date(2025, 3, 31))
        assert first.principal == Decimal("1000")  # start-date flow included
        assert last.principal == Decimal("3000")  # end-date flow included


# ── TWR engine ────────────────────────────────────────────────────────────────


class TestTimeWeightedReturn:
    def test_empty_returns_zero(self) -> None:
        """REQ-PERF-007: empty series → Decimal('0.000000')."""
        assert time_weighted_return([], [], []) == Decimal("0.000000")

    def test_simple_single_period_no_cash_flows(self) -> None:
        """REQ-PERF-007: V_begin=10000, V_end=11000, no CF, 9-day window → 10% un-annualized."""
        pts = [
            DailyPoint(
                date=date(2025, 1, 1),
                market_value=Decimal("10000"),
                principal=Decimal("10000"),
                growth=Decimal("0"),
            ),
            DailyPoint(
                date=date(2025, 1, 10),
                market_value=Decimal("11000"),
                principal=Decimal("10000"),
                growth=Decimal("1000"),
            ),
        ]
        result = time_weighted_return(pts, [], [date(2025, 1, 1)])
        assert abs(result - Decimal("0.1")) < Decimal("0.0001")

    def test_chain_link_two_periods_positive(self) -> None:
        """REQ-PERF-007: two-period chain compounds → positive TWR."""
        pts = [
            DailyPoint(date=date(2025, 1, 1), market_value=Decimal("10000"),
                       principal=Decimal("0"), growth=Decimal("0")),
            DailyPoint(date=date(2025, 2, 1), market_value=Decimal("11000"),
                       principal=Decimal("0"), growth=Decimal("0")),
            DailyPoint(date=date(2025, 3, 3), market_value=Decimal("11550"),
                       principal=Decimal("0"), growth=Decimal("0")),
        ]
        result = time_weighted_return(pts, [], [date(2025, 1, 1), date(2025, 2, 1)])
        assert result > Decimal("0")

    def test_annualizes_when_window_gte_30_days(self) -> None:
        """REQ-PERF-007: ≥30-day window triggers annualization."""
        pts = [
            DailyPoint(date=date(2025, 1, 1), market_value=Decimal("10000"),
                       principal=Decimal("0"), growth=Decimal("0")),
            DailyPoint(date=date(2026, 1, 1), market_value=Decimal("11000"),
                       principal=Decimal("0"), growth=Decimal("0")),
        ]
        result = time_weighted_return(pts, [], [date(2025, 1, 1)])
        # 1y window → annualized ≈ raw 10%
        assert abs(result - Decimal("0.1")) < Decimal("0.001")


# ── XIRR / MWR ────────────────────────────────────────────────────────────────


class TestMoneyWeightedReturn:
    def test_xirr_one_year_8_23_pct(self) -> None:
        """REQ-PERF-008: invest 10000, receive 10823 in 1y → XIRR ≈ 0.0823."""
        cfs = [CashFlow(date(2025, 1, 15), Decimal("-10000"))]
        result = money_weighted_return(cfs, Decimal("10823"), date(2026, 1, 15))
        assert result is not None
        assert abs(result - Decimal("0.0823")) < Decimal("0.0001")

    def test_xirr_multi_deposit_npv_zero(self) -> None:
        """REQ-PERF-008: multi-deposit fixture's NPV at the returned rate ≈ 0."""
        cfs = [
            CashFlow(date(2025, 1, 15), Decimal("-10000")),
            CashFlow(date(2025, 6, 15), Decimal("-5000")),
            CashFlow(date(2026, 1, 15), Decimal("0")),
        ]
        terminal_value = Decimal("16500")
        terminal_date = date(2026, 1, 15)
        t0 = date(2025, 1, 15)

        result = money_weighted_return(cfs, terminal_value, terminal_date)
        assert result is not None

        r = float(result)
        npv = sum(
            float(cf.amount) / (1.0 + r) ** ((cf.date - t0).days / 365.0) for cf in cfs
        )
        npv += float(terminal_value) / (1.0 + r) ** ((terminal_date - t0).days / 365.0)
        assert abs(npv) < 1.0  # within $1

    def test_negative_xirr(self) -> None:
        """REQ-PERF-008: returns negative Decimal when investor lost money."""
        cfs = [CashFlow(date(2025, 1, 1), Decimal("-10000"))]
        result = money_weighted_return(cfs, Decimal("9000"), date(2026, 1, 1))
        assert result is not None
        assert result < Decimal("0")

    def test_empty_cash_flows_returns_none(self) -> None:
        """REQ-PERF-008: no cash flows → None."""
        assert money_weighted_return([], Decimal("10000"), date(2026, 1, 1)) is None

    def test_all_zero_flows_returns_none(self) -> None:
        """REQ-PERF-008: all-zero flows → None."""
        cfs = [CashFlow(date(2025, 1, 1), Decimal("0"))]
        assert money_weighted_return(cfs, Decimal("0"), date(2026, 1, 1)) is None


# ── Tracked coverage ──────────────────────────────────────────────────────────


class TestTrackedValueAt:
    def test_tracked_vs_total(self, session: Session) -> None:
        """REQ-PERF-009: tracked ≤ total; tracked accounts have recent non-none tx."""
        _acct(session, "tracked-acct")
        _acct(session, "balance-only-acct")

        _tx(
            session,
            acct_id="tracked-acct",
            action=CanonicalAction.CONTRIBUTION,
            amount=Decimal("10000"),
            trade_date=date(2025, 6, 1),
            cash_flow_type=CashFlowType.EXTERNAL_IN,
        )
        _snap(
            session,
            acct_id="tracked-acct",
            symbol=None,
            as_of=datetime(2025, 6, 1),
            qty=Decimal("0"),
            market_value=Decimal("10000"),
        )
        _snap(
            session,
            acct_id="balance-only-acct",
            symbol=None,
            as_of=datetime(2025, 6, 1),
            qty=Decimal("0"),
            market_value=Decimal("5000"),
        )

        result = tracked_value_at(session, date(2025, 6, 1))
        assert result.total_value == Decimal("15000")
        assert result.tracked_value == Decimal("10000")
        assert "tracked-acct" in result.tracked_account_ids
        assert "balance-only-acct" not in result.tracked_account_ids
        assert result.tracked_begin_date == date(2025, 6, 1)


# ── Cost-basis view ───────────────────────────────────────────────────────────


class TestCostBasisView:
    def test_views_agree_on_market_value_differ_on_principal(
        self, session: Session
    ) -> None:
        """REQ-PERF-006: outside_money and cost_basis agree on MV, differ on principal."""
        _acct(session)
        _snap(
            session,
            acct_id="acct-1",
            symbol="VTI",
            as_of=datetime(2025, 6, 1),
            qty=Decimal("10"),
            market_value=Decimal("2500"),
            cost_basis=Decimal("2000"),
        )
        _price(session, symbol="VTI", trade_date=date(2025, 6, 1), close=Decimal("250.00"))
        _lot(
            session,
            acct_id="acct-1",
            symbol="VTI",
            open_date=date(2025, 1, 1),
            cost_total=Decimal("2000"),
        )
        _tx(
            session,
            acct_id="acct-1",
            action=CanonicalAction.CONTRIBUTION,
            amount=Decimal("2200"),
            trade_date=date(2025, 1, 1),
            cash_flow_type=CashFlowType.EXTERNAL_IN,
        )

        series_om = principal_growth_series(
            session,
            PortfolioScope(),
            date(2025, 6, 1),
            date(2025, 6, 1),
            view="outside_money",
        )
        series_cb = principal_growth_series(
            session,
            PortfolioScope(),
            date(2025, 6, 1),
            date(2025, 6, 1),
            view="cost_basis",
        )
        assert series_om[0].market_value == series_cb[0].market_value == Decimal("2500.00")
        assert series_om[0].principal == Decimal("2200")
        assert series_cb[0].principal == Decimal("2000")
