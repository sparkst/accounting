"""Engine unit tests + source-spec §7 regression."""
from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pytest

from src.planning.engine import (
    loan_payment,
    real_spend,
    simulate,
)
from src.planning.params import DEFAULTS, Params


def test_real_spend_flat_through_age_69() -> None:
    """While age < glide_start_age, spend stays at spend_start."""
    for age in range(49, 70):
        assert real_spend(age, DEFAULTS) == DEFAULTS.spend_start


def test_real_spend_glides_toward_floor_at_70_plus() -> None:
    """At/after glide_start_age, spend decays geometrically toward spend_floor."""
    s70 = real_spend(70, DEFAULTS)
    s75 = real_spend(75, DEFAULTS)
    s90 = real_spend(90, DEFAULTS)
    # Per source-spec §4 expected real path
    assert 240_000 < s70 < 250_000  # ~$240k at 70 (close to start)
    assert 195_000 < s75 < 210_000  # ~$201k at 75
    assert s90 < s75 < s70          # monotonic decrease
    assert s90 > DEFAULTS.spend_floor  # never below floor


def test_loan_payment_modes() -> None:
    """Three loan modes per source spec."""
    base = dataclasses.replace(DEFAULTS, loan=1_000_000.0, loan_rate=0.0675)

    p_none = dataclasses.replace(base, loan_mode="none")
    assert loan_payment(p_none) == 0.0

    p_io = dataclasses.replace(base, loan_mode="interest_only")
    assert loan_payment(p_io) == pytest.approx(67_500.0)

    p_am = dataclasses.replace(base, loan_mode="amortize10")
    # 10-yr amortizing at 6.75% on $1M ≈ $141,478/yr
    assert 140_000 < loan_payment(p_am) < 143_000


def _single_pool(**overrides: Any) -> Params:
    """Build Params matching source-spec §7 — single $7.8M pool, no two-pool split,
    no Amy wage stream (source spec has no Amy), source-spec tax_gross=1.13."""
    base = dataclasses.replace(
        DEFAULTS,
        pool_taxable=7_800_000.0,
        pool_retirement=0.0,
        amy_wage_income=0.0,
        amy_wage_years=0,
    )
    return dataclasses.replace(base, **overrides)


# Source-spec §7 published results. ±1pp tolerance (REQ-PLAN-016).
SPEC_TABLE = {
    # (ret_mean, end_age, loan, loan_mode): survival
    (0.065, 85, 0.0, "none"):                          0.71,
    (0.08,  85, 0.0, "none"):                          0.86,
    (0.10,  85, 0.0, "none"):                          0.95,
    (0.065, 90, 0.0, "none"):                          0.68,
    (0.08,  90, 0.0, "none"):                          0.83,
    (0.10,  90, 0.0, "none"):                          0.94,
    (0.065, 85, 1_000_000.0, "interest_only"):         0.56,
    (0.08,  85, 1_000_000.0, "interest_only"):         0.72,
    (0.10,  85, 1_000_000.0, "interest_only"):         0.89,
    (0.065, 85, 1_000_000.0, "amortize10"):            0.56,
    (0.08,  85, 1_000_000.0, "amortize10"):            0.73,
    (0.10,  85, 1_000_000.0, "amortize10"):            0.88,
}


@pytest.mark.parametrize(
    ("ret_mean", "end_age", "loan", "loan_mode", "expected"),
    [(rm, ea, ln, lm, surv) for (rm, ea, ln, lm), surv in SPEC_TABLE.items()],
)
def test_source_spec_section_7_regression(
    ret_mean: float, end_age: int, loan: float, loan_mode: str, expected: float
) -> None:
    """REQ-PLAN-016: source-spec §7 survival within ±1pp on every CI run."""
    p = _single_pool(
        ret_mean=ret_mean, end_age=end_age, loan=loan, loan_mode=loan_mode
    )
    r = simulate(p, seed=13)
    assert abs(r.survival - expected) <= 0.01, (
        f"ret={ret_mean} horizon={end_age} loan={loan} mode={loan_mode}: "
        f"got {r.survival:.3f}, expected {expected:.2f}±0.01"
    )


def test_determinism_byte_identical_with_seed() -> None:
    """REQ-PLAN-017: fixed seed → identical paths array across runs."""
    p = _single_pool(ret_mean=0.08)
    r1 = simulate(p, seed=42)
    r2 = simulate(p, seed=42)
    assert np.array_equal(r1.paths, r2.paths)
    assert r1.survival == r2.survival
    assert r1.percentiles == r2.percentiles


def test_two_pool_pre_59_5_drains_taxable_only() -> None:
    """REQ-PLAN-003: draws taxable-only while age < 59.5."""
    # Set up so first-year draw is large enough to be visible against starting balances.
    p = dataclasses.replace(
        DEFAULTS,
        pool_taxable=1_000_000.0,
        pool_retirement=1_000_000.0,
        spend_start=500_000.0,
        start_age=49,
        end_age=50,           # only 1 year
        ret_mean=0.0,         # no growth → easy to verify
        ret_sd=0.0,
        n_sims=1,
        amy_wage_income=0.0,  # isolate spend
        biz_income=0.0,
        ss_amount=0.0,
    )
    r = simulate(p, seed=42)
    # After one year of pure draw, retirement should be untouched at 1.0M and
    # taxable should have been reduced by spend × tax_gross_taxable (no growth).
    # The simulate API now needs to expose split balances — assert on paths
    # by convention: paths[:, 1] is the COMBINED pool at end of year 1.
    # For this test we use the two-pool split exposed via `final_taxable` /
    # `final_retirement` fields on Results (added in T5).
    assert r.paths.shape == (1, 2)
    # Retirement should be visible separately on Results in T5; verify via
    # split fields:
    assert r.final_taxable_p50 == pytest.approx(1_000_000.0 - 500_000.0 * 1.13, abs=1.0)
    assert r.final_retirement_p50 == pytest.approx(1_000_000.0, abs=1.0)


def test_two_pool_pre_59_5_drain_to_zero_marks_ruined_early() -> None:
    """REQ-PLAN-004: if taxable hits zero pre-59.5, path is ruined-early."""
    p = dataclasses.replace(
        DEFAULTS,
        pool_taxable=100_000.0,   # tiny
        pool_retirement=10_000_000.0,
        spend_start=200_000.0,    # > taxable
        start_age=49,
        end_age=51,
        ret_mean=0.0,
        ret_sd=0.0,
        n_sims=1,
        amy_wage_income=0.0,
        biz_income=0.0,
        ss_amount=0.0,
    )
    r = simulate(p, seed=42)
    assert r.ruined_early_count == 1
    # Survival counts only paths intact through horizon → 0 here.
    assert r.survival == 0.0


def test_two_pool_post_59_5_pro_rata_split() -> None:
    """REQ-PLAN-003: post-59.5 draws split pro-rata by current balance."""
    p = dataclasses.replace(
        DEFAULTS,
        pool_taxable=1_500_000.0,
        pool_retirement=500_000.0,    # 75/25 split
        spend_start=200_000.0,
        start_age=60,                 # starts post-59.5
        end_age=61,
        ret_mean=0.0,
        ret_sd=0.0,
        n_sims=1,
        amy_wage_income=0.0,
        biz_income=0.0,
        ss_amount=0.0,
    )
    r = simulate(p, seed=42)
    # spend = 200k. Gross-up is per-pool: taxable share 75% × 1.13, retirement
    # share 25% × 1.25. Total grossed draw = 200k × (0.75*1.13 + 0.25*1.25) = 200k × 1.1600
    # Taxable share of net draw: 200k × 0.75 × 1.13 = 169,500
    # Retirement share of net draw: 200k × 0.25 × 1.25 =  62,500
    assert r.final_taxable_p50 == pytest.approx(1_500_000.0 - 169_500.0, abs=1.0)
    assert r.final_retirement_p50 == pytest.approx(500_000.0 - 62_500.0, abs=1.0)
