"""Params dataclass, spec defaults, and ScenarioGrid.

Single source of truth for engine inputs. Frozen so it's safe to share across
threads / scenarios; derive variants via `dataclasses.replace` or `Scenario.apply`.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

LoanMode = Literal["none", "interest_only", "amortize10"]


@dataclass(frozen=True)
class Params:
    """All engine inputs. See spec §4 + source-spec §3 for parameter semantics."""

    # Pool — two-pool extension (spec §4.1)
    pool_taxable: float = 6_300_000.0
    pool_retirement: float = 1_500_000.0

    # Returns
    ret_mean: float = 0.08
    ret_sd: float = 0.15

    # Inflation
    inflation: float = 0.03

    # Spend path
    spend_start: float = 250_000.0
    spend_floor: float = 110_000.0
    glide_decay: float = 0.93
    glide_start_age: int = 70

    # Social Security (household; spousal decomposition deferred to sub-project 1b)
    ss_amount: float = 50_000.0
    ss_start_age: int = 67

    # Tax gross-up — per-pool (spec §4.1)
    tax_gross_taxable: float = 1.13
    tax_gross_retirement: float = 1.25

    # Loan
    loan: float = 0.0
    loan_rate: float = 0.0675
    loan_mode: LoanMode = "none"

    # Business income (Sparkry consulting)
    biz_income: float = 0.0
    biz_years: int = 0

    # Amy W-2 wages — REQ-PLAN-018
    amy_wage_income: float = 80_000.0
    amy_wage_years: int = 3

    # Retirement contributions during working years
    contrib: float = 0.0
    contrib_years: int = 0

    # QSBS / one-time exit lump sum
    exit_amount: float = 0.0
    exit_year: int | None = None

    # Age range + sim count
    start_age: int = 49
    end_age: int = 85
    n_sims: int = 8_000

    def __post_init__(self) -> None:
        if self.loan_mode not in ("none", "interest_only", "amortize10"):
            raise ValueError(
                f"loan_mode must be one of 'none' | 'interest_only' | 'amortize10'; got {self.loan_mode!r}"
            )


DEFAULTS = Params()


@dataclass(frozen=True)
class Scenario:
    """A named override set applied on top of a base Params."""

    name: str
    overrides: dict[str, Any] = field(default_factory=dict)

    def apply(self, base: Params) -> Params:
        return dataclasses.replace(base, **self.overrides)


@dataclass(frozen=True)
class ScenarioGrid:
    """A collection of named scenarios run together."""

    scenarios: tuple[Scenario, ...]

    @classmethod
    def default(cls) -> ScenarioGrid:
        """The 15 default scenarios per spec §4.3."""
        scenarios: list[Scenario] = []
        # 9 baseline cells: 3 return regimes × 3 horizons
        for ret in (0.065, 0.08, 0.10):
            ret_label = f"{ret * 100:g}"  # "6.5" "8" "10"
            for horizon in (85, 90, 95):
                scenarios.append(
                    Scenario(
                        name=f"baseline_ret{ret_label}_horizon{horizon}",
                        overrides={"ret_mean": ret, "end_age": horizon},
                    )
                )
        # +_loan / +_biz variants run at 8% / horizon 85 (the default operating point)
        scenarios.append(
            Scenario(
                name="+_loan_1m_io",
                overrides={"loan": 1_000_000.0, "loan_mode": "interest_only"},
            )
        )
        scenarios.append(
            Scenario(
                name="+_loan_1m_amort10",
                overrides={"loan": 1_000_000.0, "loan_mode": "amortize10"},
            )
        )
        scenarios.append(
            Scenario(
                name="+_biz_320k_10y",
                overrides={"biz_income": 320_000.0, "biz_years": 10},
            )
        )
        scenarios.append(
            Scenario(
                name="+_biz_320k_10y_qsbs_5m",
                overrides={
                    "biz_income": 320_000.0,
                    "biz_years": 10,
                    "exit_amount": 5_000_000.0,
                    "exit_year": 10,
                },
            )
        )
        scenarios.append(
            Scenario(
                name="+_biz_320k_10y_qsbs_10m",
                overrides={
                    "biz_income": 320_000.0,
                    "biz_years": 10,
                    "exit_amount": 10_000_000.0,
                    "exit_year": 10,
                },
            )
        )
        # Amy wage sensitivity — REQ-PLAN-018
        scenarios.append(
            Scenario(
                name="+_amy_no_wage",
                overrides={"amy_wage_income": 0.0, "amy_wage_years": 0},
            )
        )
        return cls(scenarios=tuple(scenarios))
