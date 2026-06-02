"""Monte Carlo planning engine — pure NumPy, no I/O.

Reproduces the source-spec §5 recursion (Appendix A of the source spec) as a
vectorized implementation: yearly time-step iterated over an (n_sims,) vector of
portfolio states. Each year processes all sims simultaneously via NumPy ops.

The engine is single-pool when `pool_retirement == 0`; T5 extends it to two-pool
with 59.5 access constraints. Income streams include biz_income and
amy_wage_income (REQ-PLAN-018) on top of the source-spec baseline.

Internals use np.float64 throughout for speed; Decimal precision is preserved
only at the input boundary (loaders → Params). Outputs are float — acceptable
because consumers tolerate float precision and the model's stochastic SD (15%)
dwarfs float rounding.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.planning.params import Params


def real_spend(age: int, p: Params) -> float:
    """Real (today's-dollar) spend at given age. Source-spec §4."""
    if age < p.glide_start_age:
        return p.spend_start
    n = age - (p.glide_start_age - 1)
    return p.spend_floor + (p.spend_start - p.spend_floor) * (p.glide_decay**n)


def loan_payment(p: Params) -> float:
    """Annual loan service per `loan_mode`. Source-spec Appendix A."""
    r = p.loan_rate
    if p.loan_mode == "interest_only":
        return p.loan * r
    if p.loan_mode == "amortize10":
        # standard annuity payment formula
        return p.loan * r / (1 - (1 + r) ** -10)
    return 0.0


@dataclass(frozen=True)
class Results:
    """Output of one simulate() invocation across all sims."""

    survival: float                            # mean of (final pool > 0)
    owed: float                                # loan still owed at horizon (interest_only mode)
    percentiles: dict[int, tuple[float, float, float]]  # age → (p10, p50, p90)
    paths: np.ndarray                          # shape (n_sims, yrs+1) — for diagnostics/tests
    ruined_early_count: int                    # placeholder for T5 two-pool; 0 in single-pool


def simulate(p: Params, seed: int = 42) -> Results:
    """Run n_sims Monte Carlo paths under params p. Single-pool baseline.

    Vectorized: each year processes all sims simultaneously. The per-sim
    recursion (source-spec §5 steps 1–9) is preserved exactly.
    """
    rng = np.random.default_rng(seed)
    yrs = p.end_age - p.start_age
    n = p.n_sims

    # Initialize pool vector. Single-pool baseline: treat total pool as one.
    P = np.full(n, p.pool_taxable + p.pool_retirement, dtype=np.float64)

    paths = np.zeros((n, yrs + 1), dtype=np.float64)
    paths[:, 0] = P

    pay = loan_payment(p)

    for t in range(yrs):
        age = p.start_age + t
        spend = real_spend(age, p) * (1 + p.inflation) ** t

        lc = (
            pay
            if (p.loan_mode == "interest_only" or (p.loan_mode == "amortize10" and t < 10))
            else 0.0
        )

        biz = p.biz_income if t < p.biz_years else 0.0
        amy = p.amy_wage_income if t < p.amy_wage_years else 0.0
        ss = p.ss_amount * (1 + p.inflation) ** t if age >= p.ss_start_age else 0.0
        inc = biz + amy + ss

        draw = max(spend + lc - inc, 0.0) * p.tax_gross_taxable
        P = P - draw

        if t < p.contrib_years:
            P = P + p.contrib
        if p.exit_year is not None and t == p.exit_year:
            P = P + p.exit_amount

        # One return draw per sim per year
        returns = rng.normal(p.ret_mean, p.ret_sd, size=n)
        P = P * (1 + returns)
        P = np.where(P < 0, 0.0, P)

        paths[:, t + 1] = P

    # REQ-PLAN-015
    assert np.isfinite(paths).all(), "engine produced non-finite values"

    survival = float((paths[:, -1] > 0).mean())
    owed = p.loan if p.loan_mode == "interest_only" else 0.0
    percentiles: dict[int, tuple[float, float, float]] = {}
    for a in range(p.start_age, p.end_age + 1):
        col = paths[:, a - p.start_age]
        p10, p50, p90 = np.percentile(col, [10, 50, 90])
        percentiles[a] = (float(p10), float(p50), float(p90))

    return Results(
        survival=survival,
        owed=owed,
        percentiles=percentiles,
        paths=paths,
        ruined_early_count=0,
    )
