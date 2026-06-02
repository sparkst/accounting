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

    survival: float
    owed: float
    percentiles: dict[int, tuple[float, float, float]]
    paths: np.ndarray                       # (n_sims, yrs+1) — TOTAL pool per year
    ruined_early_count: int                 # paths that exhausted taxable pre-59.5
    final_taxable_p50: float                # diagnostic: median final taxable balance
    final_retirement_p50: float             # diagnostic: median final retirement balance


def simulate(p: Params, seed: int = 42) -> Results:
    """Run n_sims Monte Carlo paths under params p. Two-pool with 59.5 access.

    Pre-59.5: all draws come from taxable (gross by tax_gross_taxable). If
    taxable goes negative, path is marked ruined-early; both pools are zeroed
    for the remaining years to preserve totals math.

    Post-59.5: draws split pro-rata by current balance; per-pool gross-up.

    Returns the SAME `paths` shape as before — total pool per year — so the
    source-spec §7 regression continues to operate on the same array.

    When pool_retirement == 0 the two-pool branch reduces exactly to single-pool
    behavior (share_r == 0, all draws from taxable regardless of age).
    """
    rng = np.random.default_rng(seed)
    yrs = p.end_age - p.start_age
    n = p.n_sims

    PT = np.full(n, p.pool_taxable, dtype=np.float64)
    PR = np.full(n, p.pool_retirement, dtype=np.float64)
    ruined_early = np.zeros(n, dtype=bool)

    paths = np.zeros((n, yrs + 1), dtype=np.float64)
    paths[:, 0] = PT + PR

    pay = loan_payment(p)
    g_t = p.tax_gross_taxable
    g_r = p.tax_gross_retirement

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
        net_need = max(spend + lc - inc, 0.0)

        if age < 59.5:
            # Taxable-only draw, grossed up.
            draw_t = net_need * g_t
            PT = PT - draw_t
            # Mark sims whose taxable went negative as ruined-early; zero both pools.
            newly_ruined = (PT < 0) & ~ruined_early
            ruined_early = ruined_early | newly_ruined
            PT = np.where(ruined_early, 0.0, PT)
            PR = np.where(ruined_early, 0.0, PR)
        else:
            # Pro-rata split by current balance. Per-pool gross-up.
            total = PT + PR
            # Avoid divide-by-zero on already-zero sims (already counted as failures).
            safe = total > 0
            share_t = np.where(safe, PT / np.where(safe, total, 1.0), 0.0)
            share_r = np.where(safe, PR / np.where(safe, total, 1.0), 0.0)
            draw_t_share = net_need * share_t * g_t
            draw_r_share = net_need * share_r * g_r
            PT = PT - draw_t_share
            PR = PR - draw_r_share
            PT = np.where(PT < 0, 0.0, PT)
            PR = np.where(PR < 0, 0.0, PR)

        if t < p.contrib_years:
            PT = PT + p.contrib
        if p.exit_year is not None and t == p.exit_year:
            # QSBS exits go into taxable.
            PT = PT + p.exit_amount

        # Apply same return to both pools (single asset assumption in v1).
        returns = rng.normal(p.ret_mean, p.ret_sd, size=n)
        PT = PT * (1 + returns)
        PR = PR * (1 + returns)
        PT = np.where(PT < 0, 0.0, PT)
        PR = np.where(PR < 0, 0.0, PR)

        paths[:, t + 1] = PT + PR

    assert np.isfinite(paths).all(), "engine produced non-finite values"

    intact = (paths[:, -1] > 0) & ~ruined_early
    survival = float(intact.mean())
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
        ruined_early_count=int(ruined_early.sum()),
        final_taxable_p50=float(np.percentile(PT, 50)),
        final_retirement_p50=float(np.percentile(PR, 50)),
    )
