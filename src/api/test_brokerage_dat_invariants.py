"""401k plan-wrapper net-worth invariant (REQ-FIX-DAT-003).

The MS 401k wrapper (is_plan_wrapper=1) and its BrokerageLink child
(parent_account_id → wrapper) must be counted exactly ONCE in net worth and in
the policy investable base — the wrapper is excluded when the child carries the
value.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.analytics.policy import compute_policy
from src.analytics.policy_config import load_policy_config
from src.models import brokerage as _b  # noqa: F401
from src.models import history as _h  # noqa: F401
from src.models import plaid as _p  # noqa: F401
from src.models.base import Base
from src.models.brokerage import Account, PositionSnapshot
from src.models.enums import AccountType, Broker, Entity
from src.reports.brokerage_summary import compute_net_worth


def _session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_plan_wrapper_pair_counted_once() -> None:
    """REQ-FIX-DAT-003: wrapper + child both snapshotted → counted once."""
    s = _session()
    wrapper = Account(
        broker=Broker.FIDELITY.value, account_number="401K",
        account_name="MS 401k", account_type=AccountType.K401.value,
        entity=Entity.PERSONAL.value, is_plan_wrapper=True,
    )
    s.add(wrapper)
    s.flush()
    child = Account(
        broker=Broker.FIDELITY.value, account_number="BLINK",
        account_name="BrokerageLink", account_type=AccountType.BROKERAGELINK.value,
        entity=Entity.PERSONAL.value, parent_account_id=wrapper.id,
    )
    s.add(child)
    s.flush()
    # Both carry a snapshot; only the child's value should count.
    s.add(PositionSnapshot(
        account_id=wrapper.id, as_of=datetime(2026, 6, 1), symbol="WRAP",
        quantity=Decimal("1"), market_value=Decimal("99999.00"),
        source_file="f", source_row_hash="w1", raw_data={},
    ))
    s.add(PositionSnapshot(
        account_id=child.id, as_of=datetime(2026, 6, 1), symbol="AMZN",
        quantity=Decimal("1"), market_value=Decimal("5000.00"),
        cost_basis=Decimal("1000.00"),
        source_file="f", source_row_hash="c1", raw_data={},
    ))
    s.commit()

    nw = compute_net_worth(s)
    # Wrapper excluded → only the child's 5000 counts (not 5000 + 99999).
    assert nw["total"] == Decimal("5000.00")
    assert nw["plan_wrapper_excluded_count"] == 1

    # Policy investable base likewise excludes the wrapper.
    result = compute_policy(s, load_policy_config(), date(2026, 7, 1))
    assert result.investable_base == Decimal("5000.00")
