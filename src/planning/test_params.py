"""Tests for the Params dataclass and spec defaults."""
from __future__ import annotations

import dataclasses

import pytest

from src.planning.params import DEFAULTS, Params, Scenario, ScenarioGrid


def test_defaults_match_source_spec() -> None:
    """Spec defaults reproduce the source-spec Appendix A PARAMS dict, two-pool
    extension, and Amy wage stream per REQ-PLAN-018."""
    p = DEFAULTS
    # Pool: source-spec total $7.8M split per §2 of source spec
    assert p.pool_taxable == 6_300_000.0
    assert p.pool_retirement == 1_500_000.0
    # Inflation / return
    assert p.inflation == 0.03
    assert p.ret_mean == 0.08
    assert p.ret_sd == 0.15
    # Spend path
    assert p.spend_start == 250_000.0
    assert p.spend_floor == 110_000.0
    assert p.glide_decay == 0.93
    assert p.glide_start_age == 70
    # SS
    assert p.ss_amount == 50_000.0
    assert p.ss_start_age == 67
    # Tax gross — two-pool per spec §4.1
    assert p.tax_gross_taxable == 1.13
    assert p.tax_gross_retirement == 1.25
    # Ages + sims
    assert p.start_age == 49
    assert p.end_age == 85
    assert p.n_sims == 8_000
    # Loan / business defaults
    assert p.loan == 0.0
    assert p.loan_rate == 0.0675
    assert p.loan_mode == "none"
    assert p.biz_income == 0.0
    assert p.biz_years == 0
    # Amy W-2 stream — REQ-PLAN-018
    assert p.amy_wage_income == 80_000.0
    assert p.amy_wage_years == 3
    # Contributions + exit
    assert p.contrib == 0.0
    assert p.contrib_years == 0
    assert p.exit_amount == 0.0
    assert p.exit_year is None


def test_params_is_frozen() -> None:
    """Params is a frozen dataclass — mutating must raise."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULTS.spend_start = 999_999  # type: ignore[misc]


def test_params_replace_works() -> None:
    """`dataclasses.replace` is the supported way to derive a Params variant."""
    p = dataclasses.replace(DEFAULTS, spend_start=300_000.0)
    assert p.spend_start == 300_000.0
    assert p.spend_floor == DEFAULTS.spend_floor  # other fields untouched


def test_loan_mode_validates() -> None:
    """loan_mode must be one of the three known values."""
    with pytest.raises(ValueError, match="loan_mode"):
        Params(**{**dataclasses.asdict(DEFAULTS), "loan_mode": "bogus"})


def test_scenario_grid_default_has_15_cells() -> None:
    """Per spec §4.3, the default grid is exactly 15 named scenarios."""
    grid = ScenarioGrid.default()
    assert len(grid.scenarios) == 15
    names = {s.name for s in grid.scenarios}
    # Spot-check a few
    assert "baseline_ret8_horizon85" in names
    assert "baseline_ret6.5_horizon95" in names
    assert "+_loan_1m_io" in names
    assert "+_biz_320k_10y_qsbs_5m" in names


def test_scenario_overrides_apply() -> None:
    """A Scenario derives a Params by applying its overrides dict."""
    base = DEFAULTS
    s = Scenario(name="test", overrides={"ret_mean": 0.10, "end_age": 90})
    p = s.apply(base)
    assert p.ret_mean == 0.10
    assert p.end_age == 90
    assert p.ret_sd == base.ret_sd
