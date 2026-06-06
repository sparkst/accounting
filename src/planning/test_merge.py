"""Tests for merge_live_into — REQ-PLAN-006 sourcing convention."""
from __future__ import annotations

import datetime as dt

import pytest

from src.planning.inputs import LiveInputs
from src.planning.merge import merge_live_into
from src.planning.params import DEFAULTS


def make_live(**overrides: object) -> LiveInputs:
    base = dict(
        pool_taxable=7_000_000.0,
        pool_retirement=2_000_000.0,
        ttm_spend=300_000.0,
        ttm_biz_income=400_000.0,
        ttm_personal_income=85_000.0,
        latest_snapshot_date=dt.date(2026, 6, 1),
        staleness_warning=None,
        ttm_tax_effective=None,
    )
    base.update(overrides)
    return LiveInputs(**base)  # type: ignore[arg-type]


def test_pool_defaults_to_live() -> None:
    """REQ-PLAN-006: pool comes from live data."""
    live = make_live()
    p = merge_live_into(DEFAULTS, live, overrides={})
    assert p.pool_taxable == 7_000_000.0
    assert p.pool_retirement == 2_000_000.0


def test_non_pool_inputs_default_to_planning() -> None:
    """REQ-PLAN-006: non-pool fields keep their planning value."""
    live = make_live()
    p = merge_live_into(DEFAULTS, live, overrides={})
    assert p.spend_start == DEFAULTS.spend_start  # NOT live.ttm_spend
    assert p.biz_income == DEFAULTS.biz_income    # NOT live.ttm_biz_income
    assert p.amy_wage_income == DEFAULTS.amy_wage_income  # NOT live.ttm_personal_income


def test_override_trumps_both() -> None:
    """REQ-PLAN-006: --override beats live AND planning."""
    live = make_live()
    p = merge_live_into(DEFAULTS, live, overrides={"pool_taxable": 1.0, "spend_start": 99_999.0})
    assert p.pool_taxable == 1.0
    assert p.spend_start == 99_999.0


def test_unknown_override_raises() -> None:
    """REQ-PLAN-006: unknown override key surfaces an error with valid keys listed."""
    live = make_live()
    with pytest.raises(ValueError, match="unknown override key.*bogus_field"):
        merge_live_into(DEFAULTS, live, overrides={"bogus_field": 1.0})
