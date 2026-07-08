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


def _included_account_ids(session: Session, as_of: date | None = None) -> set[str]:
    """Accounts counted in valuation, optionally as of a specific date.

    ``ExpectedAccount.status == "closed"`` accounts are excluded once closed.
    There is no explicit close-date column, so ``updated_at`` (the last time
    the expected_account row changed status) is the best-available proxy for
    when the account closed: with ``as_of`` given, the account is still
    counted for dates strictly before that timestamp and excluded from
    ``as_of`` on/after it. Without ``as_of`` (default), closed accounts are
    always excluded — the historical current-net-worth behavior.

    This lets :func:`compute_networth_attribution`'s coverage calc correctly
    detect an account that drops out mid-window (REQ-NWA-001 / P1-c3d)
    instead of a date-independent exclusion list under which "present at
    start, absent by end" could never be true given forward-fill valuation.
    """
    closed_rows = (
        session.query(
            ExpectedAccount.resolved_account_id, ExpectedAccount.updated_at
        )
        .filter(
            ExpectedAccount.status == "closed",
            ExpectedAccount.resolved_account_id.isnot(None),
        )
        .all()
    )
    closed: set[str] = set()
    for acct_id, updated_at in closed_rows:
        if as_of is None:
            closed.add(acct_id)
            continue
        closed_date = (
            updated_at.date() if isinstance(updated_at, datetime) else updated_at
        )
        if closed_date is None or closed_date <= as_of:
            closed.add(acct_id)
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
      in the window − Σ start value of accounts that dropped out by end, each
      netted against that same account's external flows so an account that is
      both newly-tracked/dropped AND externally funded/withdrawn in the same
      window isn't double-counted between F and C (P2-001 / P2-att1). For a
      newly-tracked account this nets only flows dated on/before its first
      snapshot (the ones actually baked into first-observed value) — flows
      dated after the first snapshot stay solely in F (P2-401).
    * Market effect M = ΔNW − F − C (residual).
    """
    state = _load_history_state(session)
    # Inclusion is computed per-date (not once, statically) so an account
    # closed mid-window is counted at `start` but dropped by `end` — see
    # `_included_account_ids` docstring (P1-c3d).
    included_start = _included_account_ids(session, as_of=start)
    included_end = _included_account_ids(session, as_of=end)

    def _vals_at(target: date, included: set[str]) -> dict[str, Decimal]:
        per = _per_account_value_at(session, target, history_state=state)
        return {
            a_id: slot["market_value"]
            for a_id, slot in per.items()
            if a_id in included
        }

    start_vals = _vals_at(start, included_start)
    end_vals = _vals_at(end, included_end)
    nw_start = sum(start_vals.values(), Decimal("0"))
    nw_end = sum(end_vals.values(), Decimal("0"))
    delta_nw = nw_end - nw_start

    # Net flows over (start, end], tracked per-account so the coverage branch
    # below can net out double-counted dollars (P2-001 / P2-att1). An account
    # counts if it was included at either boundary (covers accounts newly
    # covered by `end` or dropped by `end` but present at `start`).
    included_any = included_start | included_end
    flows = Decimal("0")
    flow_tx_count = 0
    # Per-account flows keyed with their trade_date so the new-account
    # coverage branch below can net out only the flows baked into that
    # account's *first-snapshot* value (P2-401), not the whole window's
    # flows for that account (which would double-subtract dollars dated
    # after the first snapshot but still inside the window).
    flows_by_account: dict[str, list[tuple[date, Decimal]]] = {}
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
        if tx.account_id not in included_any or tx.amount is None:
            continue
        amt = Decimal(str(tx.amount))
        flows += amt
        flow_tx_count += 1
        tx_date = (
            tx.trade_date.date()
            if isinstance(tx.trade_date, datetime)
            else tx.trade_date
        )
        flows_by_account.setdefault(tx.account_id, []).append((tx_date, amt))

    def _account_flows_total(a_id: str) -> Decimal:
        return sum(
            (amt for _d, amt in flows_by_account.get(a_id, [])), Decimal("0")
        )

    def _account_flows_through(a_id: str, as_of: date) -> Decimal:
        return sum(
            (amt for d, amt in flows_by_account.get(a_id, []) if d <= as_of),
            Decimal("0"),
        )

    # Coverage change. An account that is both newly-tracked (or dropped) AND
    # externally funded (or withdrawn) within the same window would otherwise
    # have those same dollars counted once in `flows` and again in `coverage`
    # via its first/start value — net the account's in-window flows out of
    # its coverage contribution so market_effect isn't misattributed
    # (P2-001 / P2-att1). For newly-tracked accounts, only flows dated on or
    # before that account's first-snapshot date are actually baked into
    # `first_val`; flows dated after `first` (but still within the window)
    # are already reflected in `end_val` and in the global `flows` total, so
    # netting them here too would double-subtract them and manufacture a
    # phantom market_effect swing (P2-401).
    earliest = _earliest_snapshot_by_account(session)
    coverage = Decimal("0")
    new_account_count = 0
    for a_id, v_end in end_vals.items():
        first = earliest.get(a_id)
        if first is not None and start < first <= end and a_id not in start_vals:
            first_vals = _vals_at(first, included_end)
            first_val = first_vals.get(a_id, v_end)
            coverage += first_val - _account_flows_through(a_id, first)
            new_account_count += 1
    dropped_account_count = 0
    for a_id, v_start in start_vals.items():
        if a_id not in end_vals:
            coverage -= v_start + _account_flows_total(a_id)
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
