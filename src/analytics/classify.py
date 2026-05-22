"""Cash-flow classification for brokerage transactions (REQ-PERF-002).

Every ``BrokerageTransaction`` × analytic scope → ``CashFlowType``. The
mapping table comes from
``docs/superpowers/specs/2026-05-11-performance-measurement-design.md`` §3.2.

The portfolio-scope classification is persisted via
``BrokerageTransaction.cash_flow_type`` (set by the backfill in
``scripts/backfill_cash_flow_type.py``). Account and position scopes are
computed on demand by performance queries.

Critical asymmetries (spec §3 critical point):
- A reinvested dividend is *internal* at portfolio and account scope (no
  outside money moved) but ``external_in`` at the position scope of the
  symbol that was bought.
- A transfer between two of the user's own accounts is *internal* at
  portfolio scope (no outside money) but ``external_in`` / ``external_out``
  at account scope (cash actually leaves account A and enters account B).
- ``DIVIDEND_*``, ``INTEREST``, ``CAPITAL_GAIN_*`` are *none* at every scope
  — they are growth events, not cash flows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.models.enums import CanonicalAction, CashFlowType

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from src.models.brokerage import BrokerageTransaction


class ClassifyError(ValueError):
    """Raised for any ``CanonicalAction`` not covered by the mapping table.

    The mapping in :func:`classify` is exhaustive over the enum. A new enum
    value must be added here (with a test asserting its classification) before
    rows of that action can be classified — silent default to ``none`` would
    hide misclassification bugs.
    """


# ── Scope sentinels ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PortfolioScope:
    """Scope = the entire user portfolio. No additional context needed."""


@dataclass(frozen=True, slots=True)
class AccountScope:
    """Scope = one specific brokerage account (by id)."""

    account_id: str


@dataclass(frozen=True, slots=True)
class PositionScope:
    """Scope = one symbol within one account (or across the portfolio).

    ``account_id`` may be ``None`` for portfolio-level holding views.
    """

    symbol: str
    account_id: str | None = None


Scope = PortfolioScope | AccountScope | PositionScope


# ── Static per-action mappings ───────────────────────────────────────────
#
# Three actions need pairing/signing context (TRANSFER, JOURNAL, EXCHANGE) and
# are handled procedurally. RSU_VEST also needs context (gross FMV at vest is
# the value, not the row's ``amount`` which is often 0 for the share leg).
#
# Income events (dividends, interest, capital-gain distributions) are growth,
# never cash flows — at every scope.

_PORTFOLIO_STATIC: dict[CanonicalAction, CashFlowType] = {
    CanonicalAction.BUY: CashFlowType.NONE,
    CanonicalAction.SELL: CashFlowType.NONE,
    CanonicalAction.DIVIDEND_QUALIFIED: CashFlowType.NONE,
    CanonicalAction.DIVIDEND_ORDINARY: CashFlowType.NONE,
    CanonicalAction.INTEREST: CashFlowType.NONE,
    CanonicalAction.REINVEST: CashFlowType.NONE,
    CanonicalAction.CAPITAL_GAIN_LT: CashFlowType.NONE,
    CanonicalAction.CAPITAL_GAIN_ST: CashFlowType.NONE,
    CanonicalAction.STOCK_SPLIT: CashFlowType.NONE,
    CanonicalAction.CASH_IN_LIEU: CashFlowType.NONE,
    CanonicalAction.SWEEP: CashFlowType.NONE,
    CanonicalAction.FEE: CashFlowType.NONE,
    CanonicalAction.VALUATION_ADJUSTMENT: CashFlowType.NONE,
    CanonicalAction.OTHER: CashFlowType.NONE,
    # CONTRIBUTION/DISTRIBUTION/RSU_VEST/TRANSFER/JOURNAL/EXCHANGE: handled below.
}

_POSITION_STATIC: dict[CanonicalAction, CashFlowType] = {
    # Within a position, BUY brings outside cash *to that symbol*; SELL takes
    # cash away. Reinvested dividend cash → shares is also external_in *for
    # that symbol* (cash from the sweep that wasn't there before for this
    # symbol). This is the documented portfolio-vs-position asymmetry.
    CanonicalAction.BUY: CashFlowType.EXTERNAL_IN,
    CanonicalAction.SELL: CashFlowType.EXTERNAL_OUT,
    CanonicalAction.REINVEST: CashFlowType.EXTERNAL_IN,
    # Cash-only events (dividends, interest, fees) don't touch any symbol's
    # share count, so they're ``none`` at position scope.
    CanonicalAction.DIVIDEND_QUALIFIED: CashFlowType.NONE,
    CanonicalAction.DIVIDEND_ORDINARY: CashFlowType.NONE,
    CanonicalAction.INTEREST: CashFlowType.NONE,
    CanonicalAction.CAPITAL_GAIN_LT: CashFlowType.NONE,
    CanonicalAction.CAPITAL_GAIN_ST: CashFlowType.NONE,
    CanonicalAction.CASH_IN_LIEU: CashFlowType.NONE,
    CanonicalAction.SWEEP: CashFlowType.NONE,
    CanonicalAction.FEE: CashFlowType.NONE,
    CanonicalAction.VALUATION_ADJUSTMENT: CashFlowType.NONE,
    CanonicalAction.OTHER: CashFlowType.NONE,
    # Stock splits change share count but no value change — not a cash flow.
    CanonicalAction.STOCK_SPLIT: CashFlowType.NONE,
    # CONTRIBUTION/DISTRIBUTION: cash that hasn't been allocated to a symbol
    # yet. Lands as cash; a later BUY will allocate. None at position scope.
    CanonicalAction.CONTRIBUTION: CashFlowType.NONE,
    CanonicalAction.DISTRIBUTION: CashFlowType.NONE,
    # RSU_VEST: cash represented by shares of a specific symbol; the symbol
    # gains those shares from outside money.
    CanonicalAction.RSU_VEST: CashFlowType.EXTERNAL_IN,
    # TRANSFER/JOURNAL/EXCHANGE: handled procedurally.
}


def _signed_direction(amount_signed: object) -> CashFlowType:
    """Return EXTERNAL_IN for positive amounts, EXTERNAL_OUT for negative.

    Treats None and 0 as NONE. ``amount`` is Decimal in the model but the
    importable typing surface allows any numeric.
    """
    if amount_signed is None:
        return CashFlowType.NONE
    try:
        # Decimal comparison; works for Decimal/int/float.
        if amount_signed > 0:  # type: ignore[operator]
            return CashFlowType.EXTERNAL_IN
        if amount_signed < 0:  # type: ignore[operator]
            return CashFlowType.EXTERNAL_OUT
    except TypeError as exc:
        raise ClassifyError(
            f"cannot sign-classify amount={amount_signed!r}: {exc}"
        ) from exc
    return CashFlowType.NONE


def _classify_transfer_like(tx: BrokerageTransaction, scope: Scope) -> CashFlowType:
    """TRANSFER / JOURNAL / EXCHANGE classification.

    If the transaction is paired (``paired_transaction_id`` set), the other
    side is in our DB → ``internal`` at portfolio AND account scopes (no
    outside money). At position scope it's also ``internal`` (no symbol
    shares moved in/out of *this* symbol unless the paired side touches it,
    which is rare for transfers; we treat as internal by default).

    If unpaired, fall back to amount sign: positive = ``external_in``,
    negative = ``external_out``. The auto-pair script (REQ-PERF-004) queues
    candidate pairs for the user to confirm, after which a recompute flips
    these to ``internal``.
    """
    if tx.paired_transaction_id is not None:
        return CashFlowType.INTERNAL
    return _signed_direction(tx.amount)


def _classify_contribution_distribution(
    tx: BrokerageTransaction, scope: Scope
) -> CashFlowType:
    """CONTRIBUTION = external_in; DISTRIBUTION = external_out at portfolio + account.

    At position scope these are NONE (cash, not symbol shares). Already
    encoded in ``_POSITION_STATIC``.
    """
    if tx.canonical_action == CanonicalAction.CONTRIBUTION:
        return CashFlowType.EXTERNAL_IN
    if tx.canonical_action == CanonicalAction.DISTRIBUTION:
        return CashFlowType.EXTERNAL_OUT
    raise ClassifyError(
        f"_classify_contribution_distribution called with {tx.canonical_action!r}"
    )


def _classify_rsu_vest(tx: BrokerageTransaction, scope: Scope) -> CashFlowType:
    """RSU_VEST is the one source of true outside money that doesn't look like an ACH.

    At every scope it is ``external_in`` (gross FMV at vest enters the
    portfolio / account / specific symbol). The tax-withholding sell-to-cover
    that follows is a normal ``SELL`` and classifies via ``_POSITION_STATIC``.
    """
    return CashFlowType.EXTERNAL_IN


def classify(tx: BrokerageTransaction, scope: Scope) -> CashFlowType:
    """Return the ``CashFlowType`` of ``tx`` under ``scope``.

    See module docstring for the mapping rationale. Unknown
    ``CanonicalAction`` values raise ``ClassifyError`` (no silent default).

    Account-scope behaviour for transfer-like actions: if the transfer is
    paired (i.e., the other leg also lives in our DB on another account of
    the user's), each leg is still EXTERNAL_* *to its own account* — money
    really did leave account A and enter account B. The "internal at the
    portfolio level" netting happens at the aggregation layer, not here. This
    matches the §3.3 portfolio-level netting semantic without forcing each
    leg's row to lie about its own account-scope cash movement.
    """
    action = tx.canonical_action
    try:
        action_enum = CanonicalAction(action)
    except ValueError as exc:
        raise ClassifyError(
            f"unknown CanonicalAction {action!r} on transaction id={tx.id!r}"
        ) from exc

    if isinstance(scope, PortfolioScope):
        return _classify_portfolio(tx, action_enum)
    if isinstance(scope, AccountScope):
        return _classify_account(tx, action_enum)
    if isinstance(scope, PositionScope):
        return _classify_position(tx, action_enum, scope)
    raise ClassifyError(f"unknown scope {scope!r}")


def _classify_portfolio(
    tx: BrokerageTransaction, action: CanonicalAction
) -> CashFlowType:
    if action in _PORTFOLIO_STATIC:
        return _PORTFOLIO_STATIC[action]
    if action in (
        CanonicalAction.TRANSFER,
        CanonicalAction.JOURNAL,
        CanonicalAction.EXCHANGE,
    ):
        return _classify_transfer_like(tx, PortfolioScope())
    if action in (CanonicalAction.CONTRIBUTION, CanonicalAction.DISTRIBUTION):
        return _classify_contribution_distribution(tx, PortfolioScope())
    if action == CanonicalAction.RSU_VEST:
        return _classify_rsu_vest(tx, PortfolioScope())
    raise ClassifyError(f"no portfolio-scope rule for {action!r}")


def _classify_account(
    tx: BrokerageTransaction, action: CanonicalAction
) -> CashFlowType:
    """Account-scope mirrors portfolio for most actions, but transfer-like
    rows whose pair touches a *different* account remain EXTERNAL on this
    side — money really did move *between* accounts. The portfolio-level
    aggregator nets the two legs back to zero.
    """
    if action in (
        CanonicalAction.TRANSFER,
        CanonicalAction.JOURNAL,
        CanonicalAction.EXCHANGE,
    ):
        # At account scope, even paired transfers are EXTERNAL_* on their own
        # leg — cash did leave/enter THIS account. Use the sign.
        return _signed_direction(tx.amount)
    return _classify_portfolio(tx, action)


def _classify_position(
    tx: BrokerageTransaction, action: CanonicalAction, scope: PositionScope
) -> CashFlowType:
    # Only transactions whose ``symbol`` matches ``scope.symbol`` can be a
    # cash-flow for that position; anything else is NONE.
    if tx.symbol != scope.symbol:
        return CashFlowType.NONE
    if action in _POSITION_STATIC:
        return _POSITION_STATIC[action]
    if action in (
        CanonicalAction.TRANSFER,
        CanonicalAction.JOURNAL,
        CanonicalAction.EXCHANGE,
    ):
        # An in-kind transfer of shares of this symbol is EXTERNAL_* to the
        # position even if paired (the shares moved between accounts; from a
        # single-account position perspective that's a flow).
        return _signed_direction(tx.amount)
    raise ClassifyError(f"no position-scope rule for {action!r}")
