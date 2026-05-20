"""Tests for REQ-PERF-002 — cash-flow classification at three scopes.

Covers every ``CanonicalAction`` enum value at portfolio, account, and
position scopes. Includes the portfolio-vs-position asymmetry on reinvest
(spec §3 critical-point) and the paired-vs-unpaired transfer behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest

from src.analytics.classify import (
    AccountScope,
    ClassifyError,
    PortfolioScope,
    PositionScope,
    classify,
)
from src.models.enums import CanonicalAction, CashFlowType

if TYPE_CHECKING:
    from src.models.brokerage import BrokerageTransaction


@dataclass
class _FakeTx:
    """In-memory stand-in for ``BrokerageTransaction``.

    Has only the fields ``classify`` reads, so tests don't need a DB. We
    ``cast`` it to ``BrokerageTransaction`` at the call sites — the runtime
    duck-typing works because ``classify`` only reads attributes that exist
    on both.
    """

    canonical_action: str
    amount: Decimal | None = None
    symbol: str | None = None
    paired_transaction_id: str | None = None
    id: str = "fake-tx-id"


# ── Helpers ──────────────────────────────────────────────────────────────


def _tx(action: CanonicalAction, **kwargs: object) -> BrokerageTransaction:
    fake = _FakeTx(canonical_action=action.value, **kwargs)  # type: ignore[arg-type]
    return cast("BrokerageTransaction", fake)


PORT = PortfolioScope()
ACCT = AccountScope(account_id="A1")
POS_VTI = PositionScope(symbol="VTI", account_id="A1")


# ── Per-action coverage (portfolio scope) ────────────────────────────────


@pytest.mark.parametrize(
    "action,expected",
    [
        (CanonicalAction.BUY, CashFlowType.NONE),
        (CanonicalAction.SELL, CashFlowType.NONE),
        (CanonicalAction.DIVIDEND_QUALIFIED, CashFlowType.NONE),
        (CanonicalAction.DIVIDEND_ORDINARY, CashFlowType.NONE),
        (CanonicalAction.INTEREST, CashFlowType.NONE),
        (CanonicalAction.REINVEST, CashFlowType.NONE),
        (CanonicalAction.CAPITAL_GAIN_LT, CashFlowType.NONE),
        (CanonicalAction.CAPITAL_GAIN_ST, CashFlowType.NONE),
        (CanonicalAction.STOCK_SPLIT, CashFlowType.NONE),
        (CanonicalAction.CASH_IN_LIEU, CashFlowType.NONE),
        (CanonicalAction.SWEEP, CashFlowType.NONE),
        (CanonicalAction.FEE, CashFlowType.NONE),
        (CanonicalAction.VALUATION_ADJUSTMENT, CashFlowType.NONE),
        (CanonicalAction.OTHER, CashFlowType.NONE),
    ],
)
def test_portfolio_scope_static(
    action: CanonicalAction, expected: CashFlowType
) -> None:
    """REQ-PERF-002: static actions classify the same regardless of fields."""
    assert classify(_tx(action, amount=Decimal("100.00")), PORT) == expected


def test_portfolio_contribution() -> None:
    """REQ-PERF-002: CONTRIBUTION → external_in at portfolio."""
    tx = _tx(CanonicalAction.CONTRIBUTION, amount=Decimal("5000.00"))
    assert classify(tx, PORT) == CashFlowType.EXTERNAL_IN


def test_portfolio_distribution() -> None:
    """REQ-PERF-002: DISTRIBUTION → external_out at portfolio."""
    tx = _tx(CanonicalAction.DISTRIBUTION, amount=Decimal("-1500.00"))
    assert classify(tx, PORT) == CashFlowType.EXTERNAL_OUT


def test_portfolio_rsu_vest() -> None:
    """REQ-PERF-002: RSU_VEST → external_in (gross FMV at vest)."""
    tx = _tx(CanonicalAction.RSU_VEST, amount=Decimal("12345.67"), symbol="ANTHRP")
    assert classify(tx, PORT) == CashFlowType.EXTERNAL_IN


def test_portfolio_transfer_paired_is_internal() -> None:
    """Paired transfer → internal at portfolio (no outside money)."""
    tx = _tx(
        CanonicalAction.TRANSFER,
        amount=Decimal("-1000.00"),
        paired_transaction_id="other-leg-id",
    )
    assert classify(tx, PORT) == CashFlowType.INTERNAL


def test_portfolio_transfer_unpaired_uses_sign() -> None:
    """Unpaired transfer defaults to external_*. Sign drives direction."""
    pos = _tx(CanonicalAction.TRANSFER, amount=Decimal("500.00"))
    neg = _tx(CanonicalAction.TRANSFER, amount=Decimal("-500.00"))
    assert classify(pos, PORT) == CashFlowType.EXTERNAL_IN
    assert classify(neg, PORT) == CashFlowType.EXTERNAL_OUT


def test_portfolio_journal_paired_is_internal() -> None:
    """JOURNAL works the same as TRANSFER for pairing."""
    tx = _tx(
        CanonicalAction.JOURNAL,
        amount=Decimal("-200.00"),
        paired_transaction_id="other",
    )
    assert classify(tx, PORT) == CashFlowType.INTERNAL


def test_portfolio_exchange_unpaired_uses_sign() -> None:
    tx = _tx(CanonicalAction.EXCHANGE, amount=Decimal("750.00"))
    assert classify(tx, PORT) == CashFlowType.EXTERNAL_IN


# ── Account-scope behaviour ──────────────────────────────────────────────


def test_account_scope_paired_transfer_still_external() -> None:
    """At account scope, even a paired transfer is external — money really
    did leave THIS account (and enter another).

    The portfolio-level aggregator nets the two legs back to zero. See
    classify.classify docstring.
    """
    tx = _tx(
        CanonicalAction.TRANSFER,
        amount=Decimal("-1000.00"),
        paired_transaction_id="other-leg-id",
    )
    assert classify(tx, ACCT) == CashFlowType.EXTERNAL_OUT


def test_account_scope_contribution_external_in() -> None:
    tx = _tx(CanonicalAction.CONTRIBUTION, amount=Decimal("5000.00"))
    assert classify(tx, ACCT) == CashFlowType.EXTERNAL_IN


def test_account_scope_static_actions_mirror_portfolio() -> None:
    """Actions in ``_PORTFOLIO_STATIC`` classify the same at account scope."""
    for action in (
        CanonicalAction.BUY,
        CanonicalAction.SELL,
        CanonicalAction.DIVIDEND_QUALIFIED,
        CanonicalAction.REINVEST,
        CanonicalAction.FEE,
    ):
        tx = _tx(action, amount=Decimal("10.00"))
        assert classify(tx, ACCT) == CashFlowType.NONE


# ── Position-scope behaviour ─────────────────────────────────────────────


def test_position_buy_external_in_for_symbol() -> None:
    """BUY of VTI at position(VTI) → external_in."""
    tx = _tx(CanonicalAction.BUY, symbol="VTI", amount=Decimal("-1000.00"))
    assert classify(tx, POS_VTI) == CashFlowType.EXTERNAL_IN


def test_position_buy_unrelated_symbol_is_none() -> None:
    """BUY of MSFT at position(VTI) → none (different symbol)."""
    tx = _tx(CanonicalAction.BUY, symbol="MSFT", amount=Decimal("-1000.00"))
    assert classify(tx, POS_VTI) == CashFlowType.NONE


def test_position_sell_external_out() -> None:
    tx = _tx(CanonicalAction.SELL, symbol="VTI", amount=Decimal("2000.00"))
    assert classify(tx, POS_VTI) == CashFlowType.EXTERNAL_OUT


def test_position_reinvest_external_in_critical_asymmetry() -> None:
    """REINVEST critical-point: portfolio=internal, account=internal, position(symbol)=external_in.

    Spec §3 says a reinvested dividend buys shares of a symbol using cash
    from the sweep. At the position level for that symbol, cash *entered*
    the position. At portfolio level, no outside money moved.
    """
    tx = _tx(CanonicalAction.REINVEST, symbol="VTI", amount=Decimal("-50.00"))
    assert classify(tx, PORT) == CashFlowType.NONE
    assert classify(tx, ACCT) == CashFlowType.NONE
    assert classify(tx, POS_VTI) == CashFlowType.EXTERNAL_IN


def test_position_dividend_is_none() -> None:
    """DIVIDEND is cash, not a position cash flow."""
    tx = _tx(CanonicalAction.DIVIDEND_QUALIFIED, symbol="VTI", amount=Decimal("25.00"))
    assert classify(tx, POS_VTI) == CashFlowType.NONE


def test_position_stock_split_is_none() -> None:
    """Stock splits change share count but no value change."""
    tx = _tx(CanonicalAction.STOCK_SPLIT, symbol="VTI", amount=Decimal("0"))
    assert classify(tx, POS_VTI) == CashFlowType.NONE


def test_position_rsu_vest_external_in() -> None:
    tx = _tx(CanonicalAction.RSU_VEST, symbol="VTI", amount=Decimal("0"))
    assert classify(tx, POS_VTI) == CashFlowType.EXTERNAL_IN


# ── Error paths ──────────────────────────────────────────────────────────


def test_unknown_canonical_action_raises() -> None:
    fake = _FakeTx(canonical_action="this_is_not_a_real_action")
    tx = cast("BrokerageTransaction", fake)
    with pytest.raises(ClassifyError, match="unknown CanonicalAction"):
        classify(tx, PORT)


def test_signed_direction_treats_zero_and_none_as_none() -> None:
    """Unpaired transfer with amount=0 or None falls to NONE, not external."""
    z = _tx(CanonicalAction.TRANSFER, amount=Decimal("0.00"))
    n = _tx(CanonicalAction.TRANSFER, amount=None)
    assert classify(z, PORT) == CashFlowType.NONE
    assert classify(n, PORT) == CashFlowType.NONE


# ── Enum exhaustiveness gate ─────────────────────────────────────────────


def test_every_canonical_action_classifies_at_every_scope() -> None:
    """Adding a new ``CanonicalAction`` must come with classification rules.

    This is the safety net: if someone adds an enum value without updating
    classify.py, this test fails at *every* scope.
    """
    for action in CanonicalAction:
        tx = _tx(action, symbol="VTI", amount=Decimal("1.00"))
        # Each call must succeed (no exceptions) and return a CashFlowType.
        for scope in (PORT, ACCT, POS_VTI):
            result = classify(tx, scope)
            assert isinstance(result, CashFlowType), (
                f"classify({action.value!r}, {scope!r}) returned {result!r}"
            )
