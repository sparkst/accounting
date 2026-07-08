"""Tests for investment-policy analytics (REQ-IPD-001..003, REQ-BBT)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.analytics.policy import compute_bold_bets, compute_policy
from src.analytics.policy_config import load_policy_config
from src.models import brokerage as _b  # noqa: F401
from src.models import history as _h  # noqa: F401
from src.models import plaid as _p  # noqa: F401
from src.models.base import Base
from src.models.brokerage import Account, PositionSnapshot, RealizedGainLoss
from src.models.enums import AccountType, Broker, Entity, GainLossTerm
from src.models.history import AccountTag


def _session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _acct(
    s: Any, number: str, *, atype: str = AccountType.TAXABLE.value,
    broker: str = Broker.SCHWAB.value, sheltered: bool = False, name: str | None = None,
) -> Account:
    a = Account(
        broker=broker, account_number=number, account_name=name, account_type=atype,
        entity=Entity.PERSONAL.value, tax_sheltered=sheltered,
    )
    s.add(a)
    s.flush()
    return a


def _pos(
    s: Any, acct: Account, symbol: str, mv: str, *, cost: str | None = None,
    as_of: datetime = datetime(2026, 6, 1), rh: str = "",
) -> None:
    s.add(
        PositionSnapshot(
            account_id=acct.id, as_of=as_of, symbol=symbol, quantity=Decimal("1"),
            market_value=Decimal(mv),
            cost_basis=Decimal(cost) if cost is not None else None,
            source_file="f", source_row_hash=rh or f"{acct.id}-{symbol}", raw_data={},
        )
    )


def test_concentration_includes_rsu_excludes_529() -> None:
    """REQ-IPD-001: AMZN+MSFT combined includes RSU account; 529 excluded from base."""
    s = _session()
    taxable = _acct(s, "TAX")
    rsu = _acct(s, "RSU", atype=AccountType.RSU.value)
    edu = _acct(s, "529", atype=AccountType.K529.value)
    _pos(s, taxable, "AMZN", "3000", cost="1000")
    _pos(s, rsu, "MSFT", "2000", cost="500")  # RSU counts toward combined
    _pos(s, taxable, "VTI", "5000", cost="4000")
    _pos(s, edu, "AMZN", "9999", cost="1")  # 529 excluded from investable base
    s.commit()

    cfg = load_policy_config()
    r = compute_policy(s, cfg, date(2026, 7, 1))
    assert r.investable_base == Decimal("10000")  # 3000+2000+5000, 529 excluded
    assert r.combined_value == Decimal("5000")  # AMZN 3000 + MSFT 2000
    assert r.combined_pct == Decimal("0.5")
    assert r.current_pct == Decimal("50")
    # Embedded gain surfaced per holding.
    amzn = next(c for c in r.concentration if c.symbol == "AMZN")
    assert amzn.embedded_gain == Decimal("2000")


def test_glide_headroom_at_baseline() -> None:
    """REQ-IPD-002: glide at baseline month is 51; headroom = glide − current."""
    s = _session()
    a = _acct(s, "A")
    _pos(s, a, "AMZN", "51", cost="10")
    _pos(s, a, "VTI", "49", cost="40")
    s.commit()
    cfg = load_policy_config()
    r = compute_policy(s, cfg, date(2026, 7, 1))
    assert r.glide_pct == Decimal("51")
    assert r.current_pct == Decimal("51")
    assert r.headroom_pts == Decimal("0")
    assert r.glide_series[0][1] == Decimal("51")


def test_embedded_gain_basis_missing_flagged() -> None:
    """REQ-IPD-001: null cost basis is flagged, not treated as 0."""
    s = _session()
    a = _acct(s, "A")
    _pos(s, a, "NOCB", "1000", cost=None)
    s.commit()
    cfg = load_policy_config()
    r = compute_policy(s, cfg, date(2026, 7, 1))
    row = next(c for c in r.concentration if c.symbol == "NOCB")
    assert row.basis_missing is True
    assert row.embedded_gain is None


def test_wa_excise_excludes_sheltered_and_st() -> None:
    """REQ-IPD-003: realized LT gains YTD sum taxable accounts only, LT only."""
    s = _session()
    taxable = _acct(s, "T")
    sheltered = _acct(s, "IRA", atype=AccountType.TRAD_IRA.value, sheltered=True)
    # LT gain in taxable → counts.
    s.add(RealizedGainLoss(
        account_id=taxable.id, symbol="AMZN", closed_date=date(2026, 3, 1),
        quantity=Decimal("1"), proceeds=Decimal("100"), cost_basis=Decimal("40"),
        gain_loss=Decimal("60"), lt_gain_loss=Decimal("60"), term=GainLossTerm.LONG.value,
        source_file="f", source_row_hash="r1", raw_data={},
    ))
    # ST gain in taxable → excluded.
    s.add(RealizedGainLoss(
        account_id=taxable.id, symbol="MSFT", closed_date=date(2026, 4, 1),
        quantity=Decimal("1"), proceeds=Decimal("100"), cost_basis=Decimal("90"),
        gain_loss=Decimal("10"), st_gain_loss=Decimal("10"), term=GainLossTerm.SHORT.value,
        source_file="f", source_row_hash="r2", raw_data={},
    ))
    # LT gain in sheltered → excluded.
    s.add(RealizedGainLoss(
        account_id=sheltered.id, symbol="AMZN", closed_date=date(2026, 5, 1),
        quantity=Decimal("1"), proceeds=Decimal("500"), cost_basis=Decimal("100"),
        gain_loss=Decimal("400"), lt_gain_loss=Decimal("400"), term=GainLossTerm.LONG.value,
        source_file="f", source_row_hash="r3", raw_data={},
    ))
    s.commit()
    cfg = load_policy_config()
    r = compute_policy(s, cfg, date(2026, 7, 1))
    assert r.realized_lt_gains_ytd == Decimal("60")
    assert r.excise_threshold == Decimal("270000")
    assert r.excise_threshold_headroom == Decimal("270000") - Decimal("60")


def test_bold_bets_tag_union_watchlist_cap() -> None:
    """REQ-BBT-001/002: tag ∪ watchlist union, no double-count, cap boundary."""
    s = _session()
    sleeve = _acct(s, "SLEEVE")
    s.add(AccountTag(account_id=sleeve.id, tag="bold-bet"))
    # TSLA is BOTH in a bold-bet-tagged account AND on the watchlist — count once.
    _pos(s, sleeve, "TSLA", "12000", cost="8000")
    # NVDA on watchlist only, in an untagged account.
    other = _acct(s, "OTHER")
    _pos(s, other, "NVDA", "8000", cost="5000")
    s.commit()
    cfg = load_policy_config()
    r = compute_bold_bets(s, cfg, date(2026, 7, 1))
    symbols = sorted(p.symbol for p in r.positions)
    assert symbols == ["NVDA", "TSLA"]  # no double count of TSLA
    assert r.sleeve_value == Decimal("20000")
    assert r.over_cap is False  # exactly at $20k cap → not over


def test_bold_bets_cap_breach_boundary() -> None:
    """REQ-BBT-002: $20k + $0.01 breaches the cap."""
    s = _session()
    sleeve = _acct(s, "S")
    s.add(AccountTag(account_id=sleeve.id, tag="bold-bet"))
    _pos(s, sleeve, "TSLA", "20000.01", cost="10000")
    s.commit()
    cfg = load_policy_config()
    r = compute_bold_bets(s, cfg, date(2026, 7, 1))
    assert r.over_cap is True
