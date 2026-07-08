"""Net-worth attribution — decompose ΔNW into market / flows / coverage.

REQ-NWA-001 (wealth design §12). Depends on the §1-2 total-return-consistent
valuation (dividends left in cash raise NW without appearing in flows, correctly
landing in the market-effect residual). All-Decimal, quantized at 2 on output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot
from src.models.enums import CashFlowType
from src.models.history import AccountBalanceSnapshot, ExpectedAccount
from src.reports.brokerage_summary import _load_history_state, _per_account_value_at

_Q2 = Decimal("0.01")


@dataclass
class AttributionResult:
    start: date
    end: date
    nw_start: Decimal
    nw_end: Decimal
    delta_nw: Decimal
    market_effect: Decimal
    net_flows: Decimal
    coverage_change: Decimal
    flow_tx_count: int
    new_account_count: int
    dropped_account_count: int

    def format_weekly_line(self) -> str:
        """One-line WBR summary (REQ-WBR-002 tie-out)."""
        return (
            f"NW Δ ${self.delta_nw:,.2f}: market ${self.market_effect:,.2f}, "
            f"flows ${self.net_flows:,.2f}, coverage ${self.coverage_change:,.2f}"
        )


def _included_account_ids(session: Session) -> set[str]:
    closed = {
        row[0]
        for row in session.query(ExpectedAccount.resolved_account_id)
        .filter(
            ExpectedAccount.status == "closed",
            ExpectedAccount.resolved_account_id.isnot(None),
        )
        .all()
    }
    return {
        a.id
        for a in session.query(Account).all()
        if not a.is_plan_wrapper and a.id not in closed
    }


def _earliest_snapshot_by_account(session: Session) -> dict[str, date]:
    """Min snapshot date per account across position + balance snapshots."""
    out: dict[str, date] = {}

    def _record(a_id: str | None, d: object) -> None:
        if a_id is None or d is None:
            return
        if isinstance(d, datetime):
            dd: date = d.date()
        elif isinstance(d, date):
            dd = d
        else:
            return
        prev = out.get(a_id)
        if prev is None or dd < prev:
            out[a_id] = dd

    for a_id, mn in (
        session.query(PositionSnapshot.account_id, func.min(PositionSnapshot.as_of))
        .group_by(PositionSnapshot.account_id)
        .all()
    ):
        _record(a_id, mn)
    for a_id, mn in (
        session.query(
            AccountBalanceSnapshot.account_id, func.min(AccountBalanceSnapshot.as_of)
        )
        .filter(AccountBalanceSnapshot.account_id.isnot(None))
        .group_by(AccountBalanceSnapshot.account_id)
        .all()
    ):
        _record(a_id, mn)
    return out


def compute_networth_attribution(
    session: Session, start: date, end: date
) -> AttributionResult:
    """Decompose ΔNW over ``(start, end]`` into market / flows / coverage.

    * Net flows F = Σ BrokerageTransaction.amount with cash_flow_type in
      {external_in, external_out} and trade_date in (start, end] (sign already
      positive-in / negative-out).
    * Coverage C = Σ first-observed value of accounts whose earliest snapshot is
      in the window − Σ start value of accounts that dropped out by end.
    * Market effect M = ΔNW − F − C (residual).
    """
    state = _load_history_state(session)
    included = _included_account_ids(session)

    def _vals_at(target: date) -> dict[str, Decimal]:
        per = _per_account_value_at(session, target, history_state=state)
        return {
            a_id: slot["market_value"]
            for a_id, slot in per.items()
            if a_id in included
        }

    start_vals = _vals_at(start)
    end_vals = _vals_at(end)
    nw_start = sum(start_vals.values(), Decimal("0"))
    nw_end = sum(end_vals.values(), Decimal("0"))
    delta_nw = nw_end - nw_start

    # Net flows over (start, end].
    flows = Decimal("0")
    flow_tx_count = 0
    for tx in (
        session.query(BrokerageTransaction)
        .filter(
            BrokerageTransaction.cash_flow_type.in_(
                [CashFlowType.EXTERNAL_IN.value, CashFlowType.EXTERNAL_OUT.value]
            )
        )
        .filter(BrokerageTransaction.trade_date > start)
        .filter(BrokerageTransaction.trade_date <= end)
        .all()
    ):
        if tx.account_id not in included or tx.amount is None:
            continue
        flows += Decimal(str(tx.amount))
        flow_tx_count += 1

    # Coverage change.
    earliest = _earliest_snapshot_by_account(session)
    coverage = Decimal("0")
    new_account_count = 0
    for a_id, v_end in end_vals.items():
        first = earliest.get(a_id)
        if first is not None and start < first <= end and a_id not in start_vals:
            first_vals = _vals_at(first)
            coverage += first_vals.get(a_id, v_end)
            new_account_count += 1
    dropped_account_count = 0
    for a_id, v_start in start_vals.items():
        if a_id not in end_vals:
            coverage -= v_start
            dropped_account_count += 1

    market = delta_nw - flows - coverage

    return AttributionResult(
        start=start,
        end=end,
        nw_start=nw_start.quantize(_Q2),
        nw_end=nw_end.quantize(_Q2),
        delta_nw=delta_nw.quantize(_Q2),
        market_effect=market.quantize(_Q2),
        net_flows=flows.quantize(_Q2),
        coverage_change=coverage.quantize(_Q2),
        flow_tx_count=flow_tx_count,
        new_account_count=new_account_count,
        dropped_account_count=dropped_account_count,
    )


__all__ = ["AttributionResult", "compute_networth_attribution"]
