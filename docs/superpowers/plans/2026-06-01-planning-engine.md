# Planning Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build sub-project #1 of the Sparks Retirement & Business Sustainability Model integration — a vectorized Monte Carlo engine in `src/planning/` with two-pool tax modeling, live-input loaders, persistence, one read-only API route, and a monthly launchd job. Engine reproduces source-spec §7 results within ±1pp.

**Architecture:** New domain module `src/planning/` following the repo's adapter-style layout. Pure engine (no I/O) + thin live-input layer + JSON-columned `PlanningRun` table + argparse CLI + one FastAPI route. Engine internals use `np.float64` for speed; Decimal precision is preserved only at the input boundary (loader → Params). All paths through Doppler-wrapped CLI; no secret access in engine.

**Tech Stack:** Python 3.x, NumPy (vectorized Monte Carlo), SQLAlchemy (ORM), Alembic (migration), FastAPI (route), argparse (CLI), pytest, launchd (scheduler).

**Branch:** `feat/planning-engine` (off main, currently at `b6533dc`).

**Spec:** `docs/superpowers/specs/2026-06-01-planning-engine-design.md` — every task below maps to one or more REQ-PLAN-NNN.

---

## File Map (locked before tasks)

**Create:**
- `src/planning/__init__.py` — public surface (`simulate_grid`, `load_live`, `merge_live_into`, `PlanningRun`)
- `src/planning/__main__.py` — `python -m src.planning` entry → `cli.main()`
- `src/planning/params.py` — `Params` frozen dataclass, spec defaults, `ScenarioGrid`, `Scenario`
- `src/planning/engine.py` — pure NumPy: `simulate(params, seed) → Results`, `simulate_grid(params, grid, seed) → dict[name, Results]`
- `src/planning/inputs.py` — `LiveInputs` dataclass + `load_live(session) → LiveInputs`
- `src/planning/merge.py` — `merge_live_into(params, live, overrides) → Params` (kept separate from inputs.py so it's I/O-free and trivially unit-testable)
- `src/planning/models.py` — `PlanningRun` SQLAlchemy model
- `src/planning/cli.py` — argparse: `simulate` / `show-latest` / `compare`
- `src/planning/api.py` — `router = APIRouter(prefix="/planning")` with `GET /runs/latest`
- `src/planning/scheduler.py` — single-line entry point invoked by launchd
- `src/planning/test_params.py`
- `src/planning/test_engine.py` — source-spec §7 regression + two-pool unit + determinism
- `src/planning/test_inputs.py`
- `src/planning/test_merge.py`
- `src/planning/test_models.py`
- `src/planning/test_cli.py`
- `src/planning/test_api.py`
- `src/planning/test_e2e.py` — end-to-end smoke
- `src/db/alembic/versions/<hash>_planning_runs.py` — Alembic migration creating `planning_runs` table
- `tests/fixtures/planning/__init__.py`
- `tests/fixtures/planning/build_fixture_db.py` — script that builds the fixture SQLite from a known-state schema
- `tests/fixtures/planning/accounting.fixture.db` — built artifact, committed (small, deterministic)
- `com.sparkry.planning-monthly.plist` — launchd plist in repo root
- `docs/operational/planning-engine-ops.md` — install/load/inspect instructions for the launchd job

**Modify:**
- `requirements/current.md` — append REQ-PLAN-001 … REQ-PLAN-019 section
- `src/api/main.py` — mount planning router (one import line, one `app.include_router` line)
- `src/db/alembic/env.py` — only if model import isn't already auto-picked-up; verify in T11

**Do not touch:**
- `Account`, `AccountBalanceSnapshot`, `Transaction` schemas (REQ-PLAN-005)
- Any other domain module
- Doppler config

---

## Project-Wide Quality Gates

Every commit must pass:

```bash
pytest src/planning/
ruff check src/planning/
mypy src/planning/
```

After T11 (migration), also:

```bash
alembic upgrade head     # then alembic downgrade -1 && alembic upgrade head to verify reversibility
```

Frequent commits — one per task minimum.

---

## Task 1: Add REQ-IDs to requirements

**Files:**
- Modify: `requirements/current.md` (append at end)

- [ ] **Step 1: Read the current end of the requirements file**

Run: `tail -20 requirements/current.md`

- [ ] **Step 2: Append the REQ-PLAN block**

Add this section at the end of `requirements/current.md`:

```markdown

---

## REQ-PLAN-* — Retirement & Business Sustainability Planning Engine (v1)

Source spec: `docs/superpowers/specs/2026-06-01-planning-engine-design.md`

| REQ-ID | Requirement |
|---|---|
| REQ-PLAN-001 | Monte Carlo engine reproduces source-spec §5 recursion as a vectorized NumPy implementation. |
| REQ-PLAN-002 | Engine is pure: `(Params, ScenarioGrid) → Results`, no I/O. |
| REQ-PLAN-003 | Two-pool extension: draws taxable-only while `age < 59.5`; pro-rata by current balance while `age >= 59.5`. Per-pool `tax_gross`. |
| REQ-PLAN-004 | Path is recorded as "ruined-early" if taxable hits zero pre-59.5; survival counts only intact-through-horizon paths. |
| REQ-PLAN-005 | Live-input loaders read `AccountBalanceSnapshot` and `Transaction` without modifying their schemas. |
| REQ-PLAN-006 | Pool defaults to live; other inputs default to planning; `--override` trumps both. |
| REQ-PLAN-007 | `LiveInputs` is snapshotted into every `PlanningRun` row regardless of whether values were used. |
| REQ-PLAN-008 | Default scenario grid contains the 15 scenarios listed in spec §4.3 and reproduces source-spec §7. |
| REQ-PLAN-009 | Each `simulate` invocation produces exactly one `PlanningRun` row (atomic write). |
| REQ-PLAN-010 | CLI supports `simulate`, `show-latest`, `compare`, plus `--dry-run`, `--override`, `--scenarios`, `--note`. |
| REQ-PLAN-011 | `GET /api/planning/runs/latest` returns the most recent run or 404. |
| REQ-PLAN-012 | Monthly launchd job (`com.sparkry.planning-monthly.plist`) invokes `simulate --source scheduled` on the 1st at 06:00 local. |
| REQ-PLAN-013 | Stale wealth data (>7d) → warning, run proceeds, persisted in `staleness_warning`. |
| REQ-PLAN-014 | Missing wealth data → hard fail with actionable message. |
| REQ-PLAN-015 | Engine asserts `np.isfinite(paths).all()` post-sim. |
| REQ-PLAN-016 | Source-spec §7 regression test must remain within ±1pp survival on every CI run. |
| REQ-PLAN-017 | Fixed-seed runs are byte-identical (determinism). |
| REQ-PLAN-018 | Income calculation supports `biz_income` and `amy_wage_income` as separate parameters with independent end-years; both offset draw while active. v1 defaults: `amy_wage_income=80000`, `amy_wage_years=3`. |
| REQ-PLAN-019 | `ttm_personal_income` live readout shown alongside `amy_wage_income` planning value for drift inspection (not used to override). |
```

- [ ] **Step 3: Commit**

```bash
git add requirements/current.md
git commit -m "docs(planning): add REQ-PLAN-001..019 for planning engine sub-project #1"
```

---

## Task 2: Params dataclass + spec defaults

Covers: REQ-PLAN-018 (parameter shape), foundation for everything else.

**Files:**
- Create: `src/planning/__init__.py` (empty for now)
- Create: `src/planning/params.py`
- Create: `src/planning/test_params.py`

- [ ] **Step 1: Write failing test**

Create `src/planning/test_params.py`:

```python
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
```

- [ ] **Step 2: Run test to verify FAIL**

Run: `pytest src/planning/test_params.py -v`
Expected: `ModuleNotFoundError: No module named 'src.planning.params'`

- [ ] **Step 3: Create `src/planning/__init__.py`**

```python
"""Retirement & Business Sustainability Planning Engine.

See docs/superpowers/specs/2026-06-01-planning-engine-design.md for design.
"""
```

- [ ] **Step 4: Create `src/planning/params.py`**

```python
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
    def default(cls) -> "ScenarioGrid":
        """The 15 default scenarios per spec §4.3."""
        scenarios: list[Scenario] = []
        # 9 baseline cells: 3 return regimes × 3 horizons
        for ret in (0.065, 0.08, 0.10):
            ret_label = f"{ret:g}"  # "6.5" "8" "10"
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
        return cls(scenarios=tuple(scenarios))
```

- [ ] **Step 5: Run test to verify PASS**

Run: `pytest src/planning/test_params.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 6: Quality gates + commit**

```bash
ruff check src/planning/
mypy src/planning/
git add src/planning/__init__.py src/planning/params.py src/planning/test_params.py
git commit -m "feat(planning): Params dataclass + spec defaults + ScenarioGrid (REQ-PLAN-018)"
```

---

## Task 3: Engine — single-pool baseline (foundation for §7 regression)

Covers: REQ-PLAN-001, REQ-PLAN-002, REQ-PLAN-015. Implements a vectorized NumPy port of source-spec Appendix A that operates as single-pool when `pool_retirement == 0` and `age >= 59.5` constraint is inactive. The two-pool extension lands in T5; this task lays the recursion and the §7 regression in T4 will lock its accuracy.

**Files:**
- Create: `src/planning/engine.py`
- Create: `src/planning/test_engine.py`

- [ ] **Step 1: Write failing test for `real_spend` helper**

Add to `src/planning/test_engine.py`:

```python
"""Engine unit tests + source-spec §7 regression."""
from __future__ import annotations

import math

import numpy as np
import pytest

from src.planning.engine import (
    Results,
    loan_payment,
    real_spend,
    simulate,
)
from src.planning.params import DEFAULTS


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
    import dataclasses
    base = dataclasses.replace(DEFAULTS, loan=1_000_000.0, loan_rate=0.0675)

    p_none = dataclasses.replace(base, loan_mode="none")
    assert loan_payment(p_none) == 0.0

    p_io = dataclasses.replace(base, loan_mode="interest_only")
    assert loan_payment(p_io) == pytest.approx(67_500.0)

    p_am = dataclasses.replace(base, loan_mode="amortize10")
    # 10-yr amortizing at 6.75% on $1M ≈ $141,478/yr
    assert 140_000 < loan_payment(p_am) < 143_000
```

- [ ] **Step 2: Run test to verify FAIL**

Run: `pytest src/planning/test_engine.py -v`
Expected: `ModuleNotFoundError: No module named 'src.planning.engine'`

- [ ] **Step 3: Create `src/planning/engine.py` — pure NumPy implementation**

```python
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
```

- [ ] **Step 4: Run unit tests to verify PASS**

Run: `pytest src/planning/test_engine.py::test_real_spend_flat_through_age_69 src/planning/test_engine.py::test_real_spend_glides_toward_floor_at_70_plus src/planning/test_engine.py::test_loan_payment_modes -v`
Expected: All 3 PASS.

- [ ] **Step 5: Quality gates + commit**

```bash
ruff check src/planning/
mypy src/planning/
git add src/planning/engine.py src/planning/test_engine.py
git commit -m "feat(planning): vectorized NumPy engine — single-pool baseline (REQ-PLAN-001/002/015)"
```

---

## Task 4: Source-spec §7 regression test (locks engine accuracy)

Covers: REQ-PLAN-016, REQ-PLAN-017 (determinism). The source-spec §7 published table is the canonical correctness oracle. Lock it within ±1pp per REQ-PLAN-016 — exceeding the band on any future change is a regression.

**Files:**
- Modify: `src/planning/test_engine.py` (append)

- [ ] **Step 1: Add the regression test**

Append to `src/planning/test_engine.py`:

```python
import dataclasses


def _single_pool(**overrides: object) -> Params:
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
    [(rm, ea, l, lm, surv) for (rm, ea, l, lm), surv in SPEC_TABLE.items()],
)
def test_source_spec_section_7_regression(
    ret_mean: float, end_age: int, loan: float, loan_mode: str, expected: float
) -> None:
    """REQ-PLAN-016: source-spec §7 survival within ±1pp on every CI run."""
    p = _single_pool(
        ret_mean=ret_mean, end_age=end_age, loan=loan, loan_mode=loan_mode
    )
    r = simulate(p, seed=42)
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
```

- [ ] **Step 2: Run regression tests**

Run: `pytest src/planning/test_engine.py::test_source_spec_section_7_regression -v`
Expected: All 12 parametrized cells PASS within ±1pp.

If any cell fails: do NOT relax the tolerance. The published §7 numbers used the per-sim loop in source-spec Appendix A. Vectorization preserves the underlying distribution but consumes RNG state differently — Monte Carlo at n_sims=8000 has SD on survival of about √(p(1-p)/n) ≈ 0.5pp, so ±1pp tolerance accommodates both implementations. If a cell exceeds tolerance, double-check the recursion against source spec §5 step-by-step.

- [ ] **Step 3: Run determinism test**

Run: `pytest src/planning/test_engine.py::test_determinism_byte_identical_with_seed -v`
Expected: PASS.

- [ ] **Step 4: Full file run + commit**

```bash
pytest src/planning/test_engine.py -v
ruff check src/planning/
mypy src/planning/
git add src/planning/test_engine.py
git commit -m "test(planning): lock source-spec §7 regression (REQ-PLAN-016/017)"
```

---

## Task 5: Two-pool extension (taxable + retirement, 59.5 access constraint)

Covers: REQ-PLAN-003, REQ-PLAN-004.

**Files:**
- Modify: `src/planning/engine.py` (rewrite `simulate` body)
- Modify: `src/planning/test_engine.py` (append two-pool tests)

- [ ] **Step 1: Write failing two-pool tests**

Append to `src/planning/test_engine.py`:

```python
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
```

- [ ] **Step 2: Run new tests to verify FAIL**

Run: `pytest src/planning/test_engine.py::test_two_pool_pre_59_5_drains_taxable_only -v`
Expected: FAIL — `Results` has no `final_taxable_p50` attribute, single-pool engine doesn't split.

- [ ] **Step 3: Rewrite `simulate` for two-pool + extend `Results`**

Replace the `Results` dataclass and `simulate` function in `src/planning/engine.py`:

```python
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
```

- [ ] **Step 4: Run two-pool tests to verify PASS**

Run: `pytest src/planning/test_engine.py::test_two_pool_pre_59_5_drains_taxable_only src/planning/test_engine.py::test_two_pool_pre_59_5_drain_to_zero_marks_ruined_early src/planning/test_engine.py::test_two_pool_post_59_5_pro_rata_split -v`
Expected: All 3 PASS.

- [ ] **Step 5: Re-run §7 regression to confirm no break**

Run: `pytest src/planning/test_engine.py::test_source_spec_section_7_regression -v`
Expected: All 12 cells still PASS within ±1pp. (The `_single_pool` fixture sets `pool_retirement=0`, so the two-pool branch reduces to the single-pool path identically.)

- [ ] **Step 6: Quality gates + commit**

```bash
ruff check src/planning/
mypy src/planning/
git add src/planning/engine.py src/planning/test_engine.py
git commit -m "feat(planning): two-pool extension with 59.5 access constraint (REQ-PLAN-003/004)"
```

---

## Task 6: Verify Amy wage income offsets draw correctly

Covers: REQ-PLAN-018. The engine already implements `amy_wage_income` (T3 + T5) — this task adds the explicit test that locks the behavior.

**Files:**
- Modify: `src/planning/test_engine.py` (append)

- [ ] **Step 1: Write failing test**

Append:

```python
def test_amy_wage_income_offsets_draw_during_wage_years() -> None:
    """REQ-PLAN-018: amy_wage_income offsets draw for amy_wage_years."""
    base = dataclasses.replace(
        DEFAULTS,
        pool_taxable=10_000_000.0,
        pool_retirement=0.0,
        spend_start=100_000.0,
        ss_amount=0.0,
        biz_income=0.0,
        amy_wage_income=0.0,
        amy_wage_years=0,
        start_age=49,
        end_age=51,
        ret_mean=0.0,
        ret_sd=0.0,
        n_sims=1,
    )
    no_amy = simulate(base, seed=42)

    with_amy = simulate(
        dataclasses.replace(base, amy_wage_income=80_000.0, amy_wage_years=3),
        seed=42,
    )

    # With Amy's $80k offsetting the $100k spend, net draw drops from
    # 100k×1.13 = 113k/yr to 20k×1.13 = 22.6k/yr. Over 2 years the with-Amy
    # path should retain ~$180k more.
    no_amy_final = no_amy.paths[0, -1]
    with_amy_final = with_amy.paths[0, -1]
    diff = with_amy_final - no_amy_final
    expected_diff = (80_000.0 * 1.13) * 2  # 2 years of saved draw
    assert diff == pytest.approx(expected_diff, abs=1.0)


def test_amy_wage_income_stops_at_amy_wage_years() -> None:
    """After amy_wage_years, Amy's wage no longer offsets draw."""
    p = dataclasses.replace(
        DEFAULTS,
        pool_taxable=10_000_000.0,
        pool_retirement=0.0,
        spend_start=100_000.0,
        ss_amount=0.0,
        biz_income=0.0,
        amy_wage_income=80_000.0,
        amy_wage_years=2,            # stops after t=2
        start_age=49,
        end_age=53,                  # 4 years total
        ret_mean=0.0,
        ret_sd=0.0,
        n_sims=1,
    )
    r = simulate(p, seed=42)
    # Years 0,1: net draw = 20k × 1.13 = 22.6k each
    # Years 2,3: net draw = 100k × 1.13 = 113k each
    expected_total_draw = 2 * 22_600.0 + 2 * 113_000.0
    expected_final = 10_000_000.0 - expected_total_draw
    assert r.paths[0, -1] == pytest.approx(expected_final, abs=1.0)
```

- [ ] **Step 2: Run tests to verify PASS**

Run: `pytest src/planning/test_engine.py::test_amy_wage_income_offsets_draw_during_wage_years src/planning/test_engine.py::test_amy_wage_income_stops_at_amy_wage_years -v`
Expected: Both PASS (engine already supports this from T3/T5).

- [ ] **Step 3: Commit**

```bash
git add src/planning/test_engine.py
git commit -m "test(planning): lock amy_wage_income draw-offset behavior (REQ-PLAN-018)"
```

---

## Task 7: ScenarioGrid runner — simulate_grid()

Covers: REQ-PLAN-008.

**Files:**
- Modify: `src/planning/engine.py` (add `simulate_grid`)
- Modify: `src/planning/__init__.py` (export public API)
- Modify: `src/planning/test_engine.py` (append grid test)

- [ ] **Step 1: Write failing test**

Append:

```python
from src.planning.engine import simulate_grid
from src.planning.params import ScenarioGrid


def test_simulate_grid_runs_all_15_default_scenarios() -> None:
    """REQ-PLAN-008: default grid runs all 15 cells in one invocation."""
    # Use a smaller n_sims so the test runs fast.
    base = dataclasses.replace(DEFAULTS, n_sims=500)
    out = simulate_grid(base, ScenarioGrid.default(), seed=42)
    assert len(out) == 15
    assert "baseline_ret8_horizon85" in out
    assert "+_biz_320k_10y_qsbs_10m" in out
    # Each value is a Results
    for name, r in out.items():
        assert 0.0 <= r.survival <= 1.0, f"{name}: survival out of range"
        assert r.paths.shape[0] == 500


def test_simulate_grid_deterministic_per_scenario() -> None:
    """Two grid runs with same seed produce identical per-scenario survival."""
    base = dataclasses.replace(DEFAULTS, n_sims=500)
    grid = ScenarioGrid.default()
    out1 = simulate_grid(base, grid, seed=42)
    out2 = simulate_grid(base, grid, seed=42)
    for name in out1:
        assert out1[name].survival == out2[name].survival
```

- [ ] **Step 2: Run test to verify FAIL**

Expected: `ImportError: cannot import name 'simulate_grid'`

- [ ] **Step 3: Add `simulate_grid` to `src/planning/engine.py`**

Append to `src/planning/engine.py`:

```python
from src.planning.params import ScenarioGrid as _ScenarioGrid  # type: ignore[reimported]


def simulate_grid(
    base: Params, grid: _ScenarioGrid, seed: int = 42
) -> dict[str, Results]:
    """Run every scenario in `grid` against `base`. Returns {name: Results}.

    Each scenario gets a deterministic per-name seed derived from `seed` so that
    re-running a subset of scenarios produces the same results as running the
    full grid.
    """
    out: dict[str, Results] = {}
    for i, scen in enumerate(grid.scenarios):
        p = scen.apply(base)
        # Per-scenario seed: stable across grid sizes, deterministic.
        scen_seed = seed * 1000 + i
        out[scen.name] = simulate(p, seed=scen_seed)
    return out
```

- [ ] **Step 4: Update `src/planning/__init__.py` with public exports**

Replace `src/planning/__init__.py` content:

```python
"""Retirement & Business Sustainability Planning Engine.

See docs/superpowers/specs/2026-06-01-planning-engine-design.md for design.

Public API:
    simulate, simulate_grid, Results — engine
    Params, DEFAULTS, Scenario, ScenarioGrid — params
"""
from src.planning.engine import Results, simulate, simulate_grid
from src.planning.params import DEFAULTS, Params, Scenario, ScenarioGrid

__all__ = [
    "DEFAULTS",
    "Params",
    "Results",
    "Scenario",
    "ScenarioGrid",
    "simulate",
    "simulate_grid",
]
```

- [ ] **Step 5: Run tests to verify PASS**

Run: `pytest src/planning/test_engine.py -v`
Expected: All engine tests PASS.

- [ ] **Step 6: Commit**

```bash
ruff check src/planning/
mypy src/planning/
git add src/planning/engine.py src/planning/__init__.py src/planning/test_engine.py
git commit -m "feat(planning): scenario grid runner — simulate_grid() (REQ-PLAN-008)"
```

---

## Task 8: Live-input loaders against fixture DB

Covers: REQ-PLAN-005, REQ-PLAN-007, REQ-PLAN-013, REQ-PLAN-014, REQ-PLAN-019.

**Files:**
- Create: `tests/fixtures/planning/__init__.py` (empty)
- Create: `tests/fixtures/planning/build_fixture_db.py`
- Create: `tests/fixtures/planning/accounting.fixture.db` (built artifact)
- Create: `src/planning/inputs.py`
- Create: `src/planning/test_inputs.py`

- [ ] **Step 1: Inspect the existing schema to know what columns to use**

Run: `python -c "from src.models.brokerage import Account, AccountBalanceSnapshot; import inspect; print(inspect.getsource(Account)); print('---'); print(inspect.getsource(AccountBalanceSnapshot))"`

Read the output to confirm:
- `Account.account_type` enum values (TAXABLE, BROKERAGE, IRA, ROTH, etc.)
- `AccountBalanceSnapshot` columns (`account_id`, `balance`, `snapshot_date`)
- Foreign key path: `AccountBalanceSnapshot.account_id` → `Account.id` → `Account.account_type`

If a column name differs from the spec, prefer the actual schema and note the deviation in the loader docstring.

- [ ] **Step 2: Create the fixture-building script**

Create `tests/fixtures/planning/__init__.py` empty.

Create `tests/fixtures/planning/build_fixture_db.py`:

```python
"""Build the planning-loader fixture SQLite from scratch.

Idempotent: drops + recreates the file at tests/fixtures/planning/accounting.fixture.db.
Run with: `python -m tests.fixtures.planning.build_fixture_db`.

Contents:
  - 3 Accounts: 1 taxable brokerage, 1 IRA, 1 personal checking
  - 12 months of AccountBalanceSnapshot rows (one per account per month)
  - 12 months of Transaction rows hitting personal-entity expenses,
    sparkry-entity income, and a few personal-entity income credits

Totals are deterministic and known so test assertions can be exact.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Import all models so SQLAlchemy metadata sees them
from src.db.connection import Base
from src.models.brokerage import Account, AccountBalanceSnapshot  # noqa: F401
from src.models.transaction import Transaction  # noqa: F401

FIXTURE_PATH = Path(__file__).parent / "accounting.fixture.db"


def build() -> None:
    if FIXTURE_PATH.exists():
        FIXTURE_PATH.unlink()

    engine = create_engine(f"sqlite:///{FIXTURE_PATH}")
    Base.metadata.create_all(engine)

    with Session(engine) as s:
        # Accounts — use whatever enum values the model defines; cross-check with
        # the inspection from Step 1.
        taxable = Account(
            name="Schwab Brokerage",
            account_type="TAXABLE",   # adjust to actual enum value if different
            broker="Schwab",
        )
        ira = Account(
            name="Schwab IRA",
            account_type="IRA",
            broker="Schwab",
        )
        checking = Account(
            name="Personal Checking",
            account_type="TAXABLE",   # checking is taxable for our pool purposes
            broker=None,
        )
        s.add_all([taxable, ira, checking])
        s.flush()

        # 12 months of snapshots — most recent is today (2026-06-01 for the spec)
        today = dt.date(2026, 6, 1)
        for months_ago in range(0, 12):
            snap_date = today.replace(day=1) - dt.timedelta(days=months_ago * 30)
            for acct, balance in [
                (taxable, Decimal("6300000.00")),
                (ira, Decimal("1500000.00")),
                (checking, Decimal("50000.00")),
            ]:
                s.add(
                    AccountBalanceSnapshot(
                        account_id=acct.id,
                        balance=balance,
                        snapshot_date=snap_date,
                    )
                )

        # Transactions — known annual totals:
        # personal expense: 12 × $20k = $240k (TTM spend)
        # sparkry income:   12 × $26.67k = $320k (TTM biz income)
        # personal income:  12 × $6.67k = $80k (TTM personal credits — Amy proxy)
        for months_ago in range(12):
            tx_date = today - dt.timedelta(days=months_ago * 30)
            s.add(Transaction(
                date=tx_date,
                amount=Decimal("-20000.00"),
                direction="expense",
                entity="personal",
                description=f"month-{months_ago} personal expense",
                source="fixture",
            ))
            s.add(Transaction(
                date=tx_date,
                amount=Decimal("26666.67"),
                direction="income",
                entity="sparkry",
                description=f"month-{months_ago} sparkry income",
                source="fixture",
            ))
            s.add(Transaction(
                date=tx_date,
                amount=Decimal("6666.67"),
                direction="income",
                entity="personal",
                description=f"month-{months_ago} personal income",
                source="fixture",
            ))

        s.commit()

    print(f"Built fixture: {FIXTURE_PATH}")


if __name__ == "__main__":
    build()
```

- [ ] **Step 3: Build the fixture once and commit it**

Run: `python -m tests.fixtures.planning.build_fixture_db`
Verify: `ls -la tests/fixtures/planning/accounting.fixture.db` — file exists, ~50-200 KB.

If the script errors on `account_type`, `entity`, or `direction` enum values, update the script to match the actual model definitions (from Step 1's inspection), re-run.

- [ ] **Step 4: Write failing test**

Create `src/planning/test_inputs.py`:

```python
"""Tests for live-input loaders against the planning fixture DB."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.planning.inputs import LiveInputs, load_live

FIXTURE_DB = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "planning" / "accounting.fixture.db"


@pytest.fixture
def session():
    assert FIXTURE_DB.exists(), (
        f"fixture missing — run: python -m tests.fixtures.planning.build_fixture_db"
    )
    engine = create_engine(f"sqlite:///{FIXTURE_DB}")
    with Session(engine) as s:
        yield s


def test_load_live_pool_taxable_sums_taxable_accounts(session: Session) -> None:
    """REQ-PLAN-005: pool_taxable = sum of latest balances for TAXABLE accounts."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    # Fixture has 1 taxable broker ($6.3M) + 1 checking ($50k)
    assert live.pool_taxable == pytest.approx(6_350_000.0, abs=0.01)


def test_load_live_pool_retirement_sums_retirement_accounts(session: Session) -> None:
    """REQ-PLAN-005: pool_retirement = sum of latest balances for retirement accounts."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    assert live.pool_retirement == pytest.approx(1_500_000.0, abs=0.01)


def test_load_live_ttm_spend(session: Session) -> None:
    """TTM personal expense across 12 months × $20k = $240k."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    assert live.ttm_spend == pytest.approx(240_000.0, abs=1.0)


def test_load_live_ttm_biz_income(session: Session) -> None:
    """TTM sparkry-entity income across 12 months ≈ $320k."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    assert live.ttm_biz_income == pytest.approx(320_000.0, abs=1.0)


def test_load_live_ttm_personal_income(session: Session) -> None:
    """REQ-PLAN-019: TTM personal-entity income credits (Amy wage proxy)."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    assert live.ttm_personal_income == pytest.approx(80_000.0, abs=1.0)


def test_load_live_staleness_warning_when_old(session: Session) -> None:
    """REQ-PLAN-013: if latest snapshot >7d old, staleness_warning is populated."""
    # Pretend "today" is 30 days after the most recent snapshot.
    future_today = dt.date(2026, 7, 1)
    live = load_live(session, today=future_today)
    assert live.staleness_warning is not None
    assert "30 days old" in live.staleness_warning or "day" in live.staleness_warning


def test_load_live_no_snapshots_raises(tmp_path: Path) -> None:
    """REQ-PLAN-014: missing wealth data → hard fail with actionable message."""
    from src.db.connection import Base

    empty_db = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{empty_db}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        with pytest.raises(RuntimeError, match="No AccountBalanceSnapshot"):
            load_live(s, today=dt.date(2026, 6, 1))
```

- [ ] **Step 5: Run tests to verify FAIL**

Expected: `ModuleNotFoundError: No module named 'src.planning.inputs'`

- [ ] **Step 6: Create `src/planning/inputs.py`**

```python
"""Live-input loaders.

The only file in src/planning/ that touches the register / wealth tables. If
those schemas move (e.g., during the in-flight Hetzner migration), this is the
one file that changes.

Convention (spec §4.2):
  - Pool defaults to live ("what we have" *is* reality).
  - Other inputs default to planning values; live actuals are surfaced
    alongside in LiveInputs for drift inspection.
  - ttm_personal_income is informational only (REQ-PLAN-019): never used to
    override amy_wage_income.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.brokerage import Account, AccountBalanceSnapshot
from src.models.transaction import Transaction

TAXABLE_TYPES = {"TAXABLE", "BROKERAGE"}
RETIREMENT_TYPES = {"IRA", "ROTH", "401K", "HSA", "PENSION", "ANNUITY"}
STALE_DAYS = 7


@dataclass(frozen=True)
class LiveInputs:
    """Snapshot of live data captured at engine-run time.

    Persisted into PlanningRun.live_inputs_json (REQ-PLAN-007) regardless of
    whether values were used by the engine.
    """

    pool_taxable: float
    pool_retirement: float
    ttm_spend: float
    ttm_biz_income: float
    ttm_personal_income: float
    latest_snapshot_date: dt.date
    staleness_warning: str | None
    ttm_tax_effective: float | None  # informational; None if not computable in v1


def _latest_balance_per_account(session: Session) -> list[tuple[str, float]]:
    """Returns (account_type, latest_balance) for every account with snapshots."""
    # Subquery: latest snapshot_date per account_id
    subq = (
        select(
            AccountBalanceSnapshot.account_id,
            func.max(AccountBalanceSnapshot.snapshot_date).label("latest_date"),
        )
        .group_by(AccountBalanceSnapshot.account_id)
        .subquery()
    )
    stmt = (
        select(
            Account.account_type,
            AccountBalanceSnapshot.balance,
        )
        .join(subq, subq.c.account_id == AccountBalanceSnapshot.account_id)
        .where(AccountBalanceSnapshot.snapshot_date == subq.c.latest_date)
        .join(Account, Account.id == AccountBalanceSnapshot.account_id)
    )
    return [(str(row[0]), float(row[1])) for row in session.execute(stmt)]


def _ttm_sum(
    session: Session, today: dt.date, *, entity: str, direction: str
) -> float:
    """Sum of abs(amount) for matching transactions in the trailing 365 days."""
    start = today - dt.timedelta(days=365)
    stmt = select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0)).where(
        Transaction.entity == entity,
        Transaction.direction == direction,
        Transaction.date >= start,
        Transaction.date <= today,
    )
    return float(session.execute(stmt).scalar() or 0.0)


def load_live(session: Session, today: dt.date | None = None) -> LiveInputs:
    """Read live inputs from the register + wealth tables.

    Raises RuntimeError if no AccountBalanceSnapshot rows exist (REQ-PLAN-014):
    the caller must fix wealth ingestion before a planning run is meaningful.
    """
    if today is None:
        today = dt.date.today()

    rows = _latest_balance_per_account(session)
    if not rows:
        raise RuntimeError(
            "No AccountBalanceSnapshot rows found. Run scripts/plaid_balance_sync.py "
            "or pass --override pool_taxable=... pool_retirement=... to bypass."
        )

    pool_taxable = sum(b for t, b in rows if t in TAXABLE_TYPES)
    pool_retirement = sum(b for t, b in rows if t in RETIREMENT_TYPES)

    latest_date_stmt = select(func.max(AccountBalanceSnapshot.snapshot_date))
    latest_date = session.execute(latest_date_stmt).scalar()
    assert latest_date is not None  # guaranteed by the non-empty rows check above

    age_days = (today - latest_date).days
    staleness_warning: str | None = None
    if age_days > STALE_DAYS:
        staleness_warning = (
            f"latest AccountBalanceSnapshot is {age_days} days old "
            f"(snapshot_date={latest_date.isoformat()}); pool values may be stale"
        )

    ttm_spend = _ttm_sum(session, today, entity="personal", direction="expense")
    ttm_biz_income = _ttm_sum(session, today, entity="sparkry", direction="income")
    ttm_personal_income = _ttm_sum(
        session, today, entity="personal", direction="income"
    )

    return LiveInputs(
        pool_taxable=pool_taxable,
        pool_retirement=pool_retirement,
        ttm_spend=ttm_spend,
        ttm_biz_income=ttm_biz_income,
        ttm_personal_income=ttm_personal_income,
        latest_snapshot_date=latest_date,
        staleness_warning=staleness_warning,
        ttm_tax_effective=None,  # v1: not yet computed
    )
```

- [ ] **Step 7: Run tests to verify PASS**

Run: `pytest src/planning/test_inputs.py -v`
Expected: All 7 PASS. If fixture-DB column names mismatch (e.g., `Account.account_type` is actually a Python enum), adjust the loader to call `.value` / `.name` accordingly, then re-run.

- [ ] **Step 8: Commit**

```bash
ruff check src/planning/
mypy src/planning/
git add tests/fixtures/planning/ src/planning/inputs.py src/planning/test_inputs.py
git commit -m "feat(planning): live-input loaders + fixture DB (REQ-PLAN-005/007/013/014/019)"
```

---

## Task 9: merge_live_into() — planning vs live vs overrides

Covers: REQ-PLAN-006.

**Files:**
- Create: `src/planning/merge.py`
- Create: `src/planning/test_merge.py`
- Modify: `src/planning/__init__.py` (export `merge_live_into`)

- [ ] **Step 1: Write failing tests**

Create `src/planning/test_merge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify FAIL**

Expected: `ModuleNotFoundError: No module named 'src.planning.merge'`

- [ ] **Step 3: Create `src/planning/merge.py`**

```python
"""Merge: planning defaults + live inputs + CLI overrides → final Params.

Sourcing convention (spec §4.2, REQ-PLAN-006):
  - Pool: live wins by default (live data IS reality for pool).
  - Other inputs: planning value wins by default; live is informational only.
  - --override flags trump both.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from src.planning.inputs import LiveInputs
from src.planning.params import Params


def merge_live_into(
    planning: Params, live: LiveInputs, overrides: dict[str, Any]
) -> Params:
    """Build the final Params used for a simulation.

    Order of precedence (high → low):
      1. overrides
      2. live (for pool fields only)
      3. planning
    """
    # Validate overrides early so the engineer gets a useful error.
    valid_keys = {f.name for f in dataclasses.fields(planning)}
    for k in overrides:
        if k not in valid_keys:
            raise ValueError(
                f"unknown override key {k!r}; valid keys are: {sorted(valid_keys)}"
            )

    # Step 1: pool from live.
    merged = dataclasses.replace(
        planning,
        pool_taxable=live.pool_taxable,
        pool_retirement=live.pool_retirement,
    )
    # Step 2: apply overrides.
    if overrides:
        merged = dataclasses.replace(merged, **overrides)
    return merged
```

- [ ] **Step 4: Run tests to verify PASS**

Run: `pytest src/planning/test_merge.py -v`
Expected: All 4 PASS.

- [ ] **Step 5: Update `__init__.py` exports**

Modify `src/planning/__init__.py`, adding `merge_live_into` and `LiveInputs`:

```python
"""Retirement & Business Sustainability Planning Engine.

See docs/superpowers/specs/2026-06-01-planning-engine-design.md for design.

Public API:
    simulate, simulate_grid, Results — engine
    Params, DEFAULTS, Scenario, ScenarioGrid — params
    LiveInputs, load_live — live-input loaders
    merge_live_into — input merging
"""
from src.planning.engine import Results, simulate, simulate_grid
from src.planning.inputs import LiveInputs, load_live
from src.planning.merge import merge_live_into
from src.planning.params import DEFAULTS, Params, Scenario, ScenarioGrid

__all__ = [
    "DEFAULTS",
    "LiveInputs",
    "Params",
    "Results",
    "Scenario",
    "ScenarioGrid",
    "load_live",
    "merge_live_into",
    "simulate",
    "simulate_grid",
]
```

- [ ] **Step 6: Commit**

```bash
ruff check src/planning/
mypy src/planning/
git add src/planning/merge.py src/planning/test_merge.py src/planning/__init__.py
git commit -m "feat(planning): merge_live_into — sourcing convention (REQ-PLAN-006)"
```

---

## Task 10: PlanningRun SQLAlchemy model

Covers: REQ-PLAN-009 (atomic write — single row per invocation).

**Files:**
- Create: `src/planning/models.py`
- Create: `src/planning/test_models.py`

- [ ] **Step 1: Write failing test**

Create `src/planning/test_models.py`:

```python
"""Tests for PlanningRun model — schema + JSON round-trip."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.connection import Base
from src.planning.models import PlanningRun


@pytest.fixture
def session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_planning_run_round_trip(session: Session) -> None:
    """Write a PlanningRun, read it back — JSON columns preserve nesting."""
    row = PlanningRun(
        run_at=dt.datetime(2026, 6, 1, 6, 0, 0),
        source="cli",
        params_json={"pool_taxable": 6_300_000.0, "ret_mean": 0.08, "nested": {"k": 1}},
        live_inputs_json={"pool_taxable": 6_350_000.0, "ttm_spend": 240_000.0},
        scenarios_json={
            "baseline_ret8_horizon85": {
                "survival": 0.86,
                "owed": 0.0,
                "percentiles": {"85": [0.0, 28_000_000.0, 136_000_000.0]},
            }
        },
        notes="initial test",
    )
    session.add(row)
    session.commit()

    fetched = session.query(PlanningRun).one()
    assert fetched.params_json["pool_taxable"] == 6_300_000.0
    assert fetched.params_json["nested"]["k"] == 1
    assert fetched.scenarios_json["baseline_ret8_horizon85"]["survival"] == 0.86
    assert fetched.source == "cli"
    assert fetched.notes == "initial test"


def test_planning_run_source_constrained() -> None:
    """Source must be one of 'cli' | 'scheduled' | 'api' — enforced at app level."""
    # Application-level validation only; SQLite doesn't enforce. Just confirm
    # the constant set exists for the CLI to validate against.
    from src.planning.models import VALID_SOURCES
    assert VALID_SOURCES == frozenset({"cli", "scheduled", "api"})
```

- [ ] **Step 2: Run test to verify FAIL**

Expected: `ModuleNotFoundError: No module named 'src.planning.models'`

- [ ] **Step 3: Create `src/planning/models.py`**

```python
"""PlanningRun SQLAlchemy model.

One row per simulate() invocation. JSON columns (not normalized) — consumers
are humans reading reports and future Claude analyses; flexibility beats query
efficiency for ~12 scheduled runs/year + ad-hoc volume.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.connection import Base

VALID_SOURCES = frozenset({"cli", "scheduled", "api"})


class PlanningRun(Base):
    __tablename__ = "planning_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    live_inputs_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    scenarios_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=dt.datetime.utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=dt.datetime.utcnow,
        onupdate=dt.datetime.utcnow,
    )
```

- [ ] **Step 4: Run tests to verify PASS**

Run: `pytest src/planning/test_models.py -v`
Expected: Both PASS.

- [ ] **Step 5: Commit**

```bash
ruff check src/planning/
mypy src/planning/
git add src/planning/models.py src/planning/test_models.py
git commit -m "feat(planning): PlanningRun model — JSON columns for params/live/scenarios"
```

---

## Task 11: Alembic migration for planning_runs table

**Files:**
- Create: `src/db/alembic/versions/<hash>_planning_runs.py`
- Possibly modify: `src/db/alembic/env.py` (verify model is auto-imported)

- [ ] **Step 1: Verify the model is auto-imported by alembic env**

Read `src/db/alembic/env.py` and find the line that imports project models (usually `from src.models import *` or explicit imports near the `target_metadata = Base.metadata` line). Add `from src.planning.models import PlanningRun  # noqa: F401` if not already covered.

If `env.py` imports a barrel module that imports everything under `src/models/`, the planning model won't be picked up because it's under `src/planning/`, not `src/models/`. Add the explicit import.

- [ ] **Step 2: Generate the migration**

Run:
```bash
doppler run -- alembic revision --autogenerate -m "add_planning_runs_table"
```

Inspect the generated file in `src/db/alembic/versions/`. Verify it contains ONLY:
- `op.create_table("planning_runs", ...)` with the expected columns
- `op.drop_table("planning_runs")` in `downgrade()`

If the generator also added anything else (drops, renames, alterations on unrelated tables), that indicates schema drift unrelated to this change — DO NOT commit those. Edit the migration to keep only the planning_runs create/drop.

- [ ] **Step 3: Apply + verify reversibility**

Run:
```bash
doppler run -- alembic upgrade head
doppler run -- alembic downgrade -1
doppler run -- alembic upgrade head
```
All three should succeed cleanly.

- [ ] **Step 4: Commit**

```bash
git add src/db/alembic/versions/*planning_runs.py
# Only commit env.py if it was modified
git diff --cached --stat
git commit -m "feat(db): alembic migration — create planning_runs table"
```

---

## Task 12: CLI — `simulate` command (end-to-end glue)

Covers: REQ-PLAN-009 (atomic write), REQ-PLAN-010 (CLI surface).

**Files:**
- Create: `src/planning/cli.py`
- Create: `src/planning/__main__.py`
- Create: `src/planning/test_cli.py`

- [ ] **Step 1: Write failing test**

Create `src/planning/test_cli.py`:

```python
"""Tests for the planning CLI."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.db.connection import Base
from src.planning.cli import main as cli_main
from src.planning.models import PlanningRun

FIXTURE_DB = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "planning" / "accounting.fixture.db"


@pytest.fixture
def db_with_planning_table(tmp_path: Path):
    """Copy the fixture DB and add the planning_runs table for write tests."""
    import shutil

    target = tmp_path / "test.db"
    shutil.copy(FIXTURE_DB, target)
    engine = create_engine(f"sqlite:///{target}")
    Base.metadata.create_all(engine)  # ensures planning_runs table exists
    return target


def test_simulate_writes_exactly_one_row(db_with_planning_table: Path) -> None:
    """REQ-PLAN-009: one simulate invocation → exactly one PlanningRun row."""
    with patch("src.planning.cli._open_session") as open_session:
        engine = create_engine(f"sqlite:///{db_with_planning_table}")
        Sess = sessionmaker(bind=engine)
        open_session.return_value = Sess()

        rc = cli_main(["simulate", "--n-sims", "300"])
        assert rc == 0

    # Verify exactly one row
    engine = create_engine(f"sqlite:///{db_with_planning_table}")
    with Session(engine) as s:
        rows = s.query(PlanningRun).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "cli"
        # All 15 scenarios should be in scenarios_json
        assert len(row.scenarios_json) == 15
        # live_inputs_json should have the fixture-derived values
        assert row.live_inputs_json["pool_taxable"] > 0
        assert row.live_inputs_json["ttm_spend"] == pytest.approx(240_000.0, abs=1.0)


def test_simulate_dry_run_writes_nothing(db_with_planning_table: Path) -> None:
    """REQ-PLAN-010: --dry-run does not persist."""
    with patch("src.planning.cli._open_session") as open_session:
        engine = create_engine(f"sqlite:///{db_with_planning_table}")
        Sess = sessionmaker(bind=engine)
        open_session.return_value = Sess()

        rc = cli_main(["simulate", "--dry-run", "--n-sims", "300"])
        assert rc == 0

    engine = create_engine(f"sqlite:///{db_with_planning_table}")
    with Session(engine) as s:
        assert s.query(PlanningRun).count() == 0


def test_simulate_override_unknown_key_exits_nonzero(
    db_with_planning_table: Path,
) -> None:
    """REQ-PLAN-010: unknown override key → non-zero exit with helpful message."""
    with patch("src.planning.cli._open_session") as open_session:
        engine = create_engine(f"sqlite:///{db_with_planning_table}")
        Sess = sessionmaker(bind=engine)
        open_session.return_value = Sess()

        rc = cli_main(
            ["simulate", "--override", "bogus_field=1", "--n-sims", "100"]
        )
        assert rc != 0


def test_simulate_with_note_persists_it(db_with_planning_table: Path) -> None:
    """REQ-PLAN-010: --note tags the persisted run."""
    with patch("src.planning.cli._open_session") as open_session:
        engine = create_engine(f"sqlite:///{db_with_planning_table}")
        Sess = sessionmaker(bind=engine)
        open_session.return_value = Sess()

        rc = cli_main(["simulate", "--note", "after-Schwab-rebalance", "--n-sims", "300"])
        assert rc == 0

    engine = create_engine(f"sqlite:///{db_with_planning_table}")
    with Session(engine) as s:
        row = s.query(PlanningRun).one()
        assert row.notes == "after-Schwab-rebalance"
```

- [ ] **Step 2: Run tests to verify FAIL**

Expected: `ModuleNotFoundError: No module named 'src.planning.cli'`

- [ ] **Step 3: Create `src/planning/cli.py`**

```python
"""Planning CLI — `python -m src.planning <subcommand>`.

Subcommands:
    simulate     run the engine + persist (default) or --dry-run
    show-latest  pretty-print the most recent PlanningRun
    compare      diff survival across runs since a date
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import sys
from typing import Any

from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.planning.engine import simulate_grid
from src.planning.inputs import load_live
from src.planning.merge import merge_live_into
from src.planning.models import PlanningRun
from src.planning.params import DEFAULTS, Params, ScenarioGrid

logger = logging.getLogger(__name__)

VALID_SOURCES = ("cli", "scheduled", "api")


def _open_session() -> Session:
    """Indirection so tests can monkeypatch DB access."""
    return SessionLocal()


def _parse_overrides(items: list[str]) -> dict[str, Any]:
    """Parse `--override key=value` pairs into a dict.

    Values are JSON-loaded so the user can pass numbers, true/false, or
    quoted strings. Unknown keys are validated downstream in merge_live_into.
    """
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--override expects KEY=VALUE; got {item!r}")
        k, v = item.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v  # treat as string
    return out


def _serialize_params(p: Params) -> dict[str, Any]:
    return dataclasses.asdict(p)


def _serialize_live(live: object) -> dict[str, Any]:
    d = dataclasses.asdict(live)
    # date → ISO string for JSON
    if isinstance(d.get("latest_snapshot_date"), dt.date):
        d["latest_snapshot_date"] = d["latest_snapshot_date"].isoformat()
    return d


def _serialize_results(results: dict[str, Any]) -> dict[str, Any]:
    """Strip non-JSON-serializable fields (paths ndarray) and convert tuples."""
    out: dict[str, Any] = {}
    for name, r in results.items():
        out[name] = {
            "survival": r.survival,
            "owed": r.owed,
            "ruined_early_count": r.ruined_early_count,
            "final_taxable_p50": r.final_taxable_p50,
            "final_retirement_p50": r.final_retirement_p50,
            "percentiles": {str(age): list(pcts) for age, pcts in r.percentiles.items()},
        }
    return out


def _cmd_simulate(args: argparse.Namespace) -> int:
    overrides = _parse_overrides(args.override)
    if args.n_sims:
        overrides["n_sims"] = args.n_sims
    source = args.source if args.source in VALID_SOURCES else "cli"

    sess = _open_session()
    try:
        try:
            live = load_live(sess, today=dt.date.today())
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

        if live.staleness_warning:
            print(f"WARNING: {live.staleness_warning}", file=sys.stderr)

        try:
            params = merge_live_into(DEFAULTS, live, overrides)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

        grid = ScenarioGrid.default()
        if args.scenarios:
            wanted = set(args.scenarios.split(","))
            grid = ScenarioGrid(
                scenarios=tuple(s for s in grid.scenarios if s.name in wanted)
            )
            if not grid.scenarios:
                print(
                    f"ERROR: no scenarios matched {args.scenarios!r}",
                    file=sys.stderr,
                )
                return 2

        results = simulate_grid(params, grid, seed=args.seed)

        # Pretty summary to stdout
        print(f"Planning run @ {dt.datetime.utcnow().isoformat()}Z (source={source})")
        print(f"  pool_taxable={params.pool_taxable:,.0f}  pool_retirement={params.pool_retirement:,.0f}")
        if live.staleness_warning:
            print(f"  ⚠ {live.staleness_warning}")
        print()
        print(f"  {'scenario':<45} survival")
        for name in sorted(results):
            r = results[name]
            marker = " ⚠" if r.ruined_early_count > 0 else ""
            print(f"  {name:<45} {r.survival:>6.1%}{marker}")
        print()
        print(f"  live drift (informational):")
        print(f"    ttm_spend         = {live.ttm_spend:,.0f}   (planning spend_start={params.spend_start:,.0f})")
        print(f"    ttm_biz_income    = {live.ttm_biz_income:,.0f}   (planning biz_income={params.biz_income:,.0f})")
        print(f"    ttm_personal_income = {live.ttm_personal_income:,.0f}   (planning amy_wage_income={params.amy_wage_income:,.0f})")

        if args.dry_run:
            print("\n[dry-run — not persisting]")
            return 0

        row = PlanningRun(
            run_at=dt.datetime.utcnow(),
            source=source,
            params_json=_serialize_params(params),
            live_inputs_json=_serialize_live(live),
            scenarios_json=_serialize_results(results),
            notes=args.note,
        )
        sess.add(row)
        sess.commit()
        print(f"\n[persisted as PlanningRun id={row.id}]")
        return 0
    finally:
        sess.close()


def _cmd_show_latest(args: argparse.Namespace) -> int:
    sess = _open_session()
    try:
        row = (
            sess.query(PlanningRun)
            .order_by(PlanningRun.run_at.desc())
            .first()
        )
        if row is None:
            print("no planning runs yet — try `simulate` first")
            return 1
        print(f"Run id={row.id}  run_at={row.run_at.isoformat()}Z  source={row.source}")
        if row.notes:
            print(f"Notes: {row.notes}")
        print()
        print(f"  {'scenario':<45} survival")
        for name in sorted(row.scenarios_json):
            r = row.scenarios_json[name]
            print(f"  {name:<45} {r['survival']:>6.1%}")
        return 0
    finally:
        sess.close()


def _cmd_compare(args: argparse.Namespace) -> int:
    since = dt.date.fromisoformat(args.since)
    sess = _open_session()
    try:
        rows = (
            sess.query(PlanningRun)
            .filter(PlanningRun.run_at >= dt.datetime.combine(since, dt.time.min))
            .order_by(PlanningRun.run_at.asc())
            .all()
        )
        if len(rows) < 2:
            print(f"need ≥2 runs since {args.since} to compare; found {len(rows)}")
            return 1

        first = rows[0]
        last = rows[-1]
        print(f"Comparing  {first.run_at.date()}  →  {last.run_at.date()}")
        print()
        print(f"  {'scenario':<45} {'first':>8} {'last':>8} {'Δ':>8}")
        for name in sorted(last.scenarios_json):
            if name not in first.scenarios_json:
                continue
            sv_first = first.scenarios_json[name]["survival"]
            sv_last = last.scenarios_json[name]["survival"]
            delta = sv_last - sv_first
            arrow = "↑" if delta > 0 else "↓" if delta < 0 else " "
            print(f"  {name:<45} {sv_first:>7.1%} {sv_last:>7.1%} {delta:>+7.1%} {arrow}")
        return 0
    finally:
        sess.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.planning")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sim = sub.add_parser("simulate", help="run the engine and persist")
    sim.add_argument("--dry-run", action="store_true", help="do not persist")
    sim.add_argument(
        "--override", action="append", default=[],
        help="KEY=VALUE override (repeatable); JSON-parsed",
    )
    sim.add_argument(
        "--scenarios", default="",
        help="comma-separated subset of scenario names",
    )
    sim.add_argument("--note", default=None, help="tag the persisted run")
    sim.add_argument("--source", default="cli", choices=VALID_SOURCES)
    sim.add_argument("--n-sims", type=int, default=None, help="override n_sims")
    sim.add_argument("--seed", type=int, default=42)
    sim.set_defaults(func=_cmd_simulate)

    show = sub.add_parser("show-latest", help="pretty-print most recent run")
    show.set_defaults(func=_cmd_show_latest)

    cmp = sub.add_parser("compare", help="diff survival across runs since a date")
    cmp.add_argument("--since", required=True, help="ISO date, e.g. 2026-01-01")
    cmp.set_defaults(func=_cmd_compare)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Create `src/planning/__main__.py`**

```python
"""Entry point for `python -m src.planning`."""
from src.planning.cli import main

raise SystemExit(main())
```

- [ ] **Step 5: Run tests to verify PASS**

Run: `pytest src/planning/test_cli.py -v`
Expected: All 4 PASS.

- [ ] **Step 6: Manual smoke**

Run: `doppler run -- python -m src.planning simulate --dry-run --n-sims 500`
Expected: Pretty summary printed; "[dry-run — not persisting]"; exit 0.

- [ ] **Step 7: Quality gates + commit**

```bash
ruff check src/planning/
mypy src/planning/
git add src/planning/cli.py src/planning/__main__.py src/planning/test_cli.py
git commit -m "feat(planning): CLI — simulate / show-latest / compare (REQ-PLAN-009/010)"
```

---

## Task 13: API route — GET /api/planning/runs/latest

Covers: REQ-PLAN-011.

**Files:**
- Create: `src/planning/api.py`
- Create: `src/planning/test_api.py`
- Modify: `src/api/main.py` (mount the router)

- [ ] **Step 1: Write failing test**

Create `src/planning/test_api.py`:

```python
"""Tests for GET /api/planning/runs/latest."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import get_db
from src.api.main import app
from src.db.connection import Base
from src.planning.models import PlanningRun


@pytest.fixture
def client(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)

    def override_get_db():
        s = Sess()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), Sess
    finally:
        app.dependency_overrides.clear()


def test_latest_returns_404_when_empty(client) -> None:
    """REQ-PLAN-011: 404 when no runs exist."""
    tc, _ = client
    resp = tc.get("/api/planning/runs/latest", headers={"X-API-Key": "test"})
    # If your auth dependency rejects missing key with 401, replace assertion
    # with a request that includes a valid key — see existing test_invoices.py
    # for the project convention.
    assert resp.status_code in (404, 401)  # 401 if auth enforced before route


def test_latest_returns_most_recent(client) -> None:
    """REQ-PLAN-011: returns the most recent run."""
    tc, Sess = client
    with Sess() as s:
        old = PlanningRun(
            run_at=dt.datetime(2026, 1, 1),
            source="cli",
            params_json={},
            live_inputs_json={},
            scenarios_json={"baseline_ret8_horizon85": {"survival": 0.80}},
            notes=None,
        )
        new = PlanningRun(
            run_at=dt.datetime(2026, 6, 1),
            source="scheduled",
            params_json={},
            live_inputs_json={},
            scenarios_json={"baseline_ret8_horizon85": {"survival": 0.86}},
            notes="month tick",
        )
        s.add_all([old, new])
        s.commit()

    resp = tc.get("/api/planning/runs/latest", headers={"X-API-Key": "test"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "scheduled"
    assert payload["notes"] == "month tick"
    assert payload["scenarios"]["baseline_ret8_horizon85"]["survival"] == 0.86
```

- [ ] **Step 2: Run test to verify FAIL**

Expected: `ModuleNotFoundError: No module named 'src.planning.api'`

- [ ] **Step 3: Create `src/planning/api.py`**

```python
"""GET /api/planning/runs/latest — read-only single endpoint for v1."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.planning.models import PlanningRun

router = APIRouter(prefix="/planning", tags=["planning"])


@router.get("/runs/latest")
def get_latest_run(db: Session = Depends(get_db)) -> dict[str, Any]:
    row = (
        db.query(PlanningRun)
        .order_by(PlanningRun.run_at.desc())
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no planning runs yet")
    return {
        "id": row.id,
        "run_at": row.run_at.isoformat(),
        "source": row.source,
        "params": row.params_json,
        "live_inputs": row.live_inputs_json,
        "scenarios": row.scenarios_json,
        "notes": row.notes,
    }
```

- [ ] **Step 4: Mount the router in `src/api/main.py`**

Add the import alongside other route imports:

```python
from src.planning.api import router as planning_router
```

Add the `app.include_router` line in the same block as the other routers (after `vendor_rules_router` is a safe alphabetical-ish spot):

```python
app.include_router(planning_router, prefix="/api", dependencies=_auth)
```

- [ ] **Step 5: Run tests to verify PASS**

Run: `pytest src/planning/test_api.py -v`
Expected: Both PASS. If the 404 test gets 401 instead, the project's API auth is rejecting unkeyed requests before the route runs — update the test to include a valid header (look at `src/api/test_invoices.py` for the convention).

- [ ] **Step 6: Commit**

```bash
ruff check src/planning/ src/api/main.py
mypy src/planning/ src/api/main.py
git add src/planning/api.py src/planning/test_api.py src/api/main.py
git commit -m "feat(planning): GET /api/planning/runs/latest (REQ-PLAN-011)"
```

---

## Task 14: Launchd plist for monthly job

Covers: REQ-PLAN-012.

**Files:**
- Create: `com.sparkry.planning-monthly.plist` (repo root)
- Create: `docs/operational/planning-engine-ops.md`
- Create: `src/planning/scheduler.py`

- [ ] **Step 1: Create `src/planning/scheduler.py`**

```python
"""Launchd entry point for the monthly planning run.

Invoked by com.sparkry.planning-monthly.plist on the 1st of each month at 06:00.
Just calls the CLI with --source scheduled so the persisted row is tagged
correctly.
"""
from __future__ import annotations

import sys

from src.planning.cli import main


def run() -> int:
    return main(["simulate", "--source", "scheduled"])


if __name__ == "__main__":
    sys.exit(run())
```

- [ ] **Step 2: Create `com.sparkry.planning-monthly.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sparkry.planning-monthly</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/travis/SGDrive/dev/accounting/.venv/bin/python3</string>
    <string>-m</string>
    <string>src.planning.scheduler</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/travis/SGDrive/dev/accounting</string>

  <!-- Run on the 1st of each month at 06:00 -->
  <key>StartCalendarInterval</key>
  <dict>
    <key>Day</key>
    <integer>1</integer>
    <key>Hour</key>
    <integer>6</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>/Users/travis/Library/Logs/com.sparkry.planning-monthly.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/travis/Library/Logs/com.sparkry.planning-monthly.error.log</string>

  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
```

- [ ] **Step 3: Create operator docs**

Create `docs/operational/planning-engine-ops.md`:

```markdown
# Planning Engine — Operator Notes

Sub-project #1 of the Sparks Retirement & Business Sustainability Model
integration. See `docs/superpowers/specs/2026-06-01-planning-engine-design.md`
for the design.

## Monthly scheduled job

`com.sparkry.planning-monthly.plist` runs on the 1st of each month at 06:00.

### Install

```bash
cp com.sparkry.planning-monthly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sparkry.planning-monthly.plist
launchctl list | grep planning   # confirm loaded
```

### Run on demand

```bash
launchctl start com.sparkry.planning-monthly
# Or skip launchd and run directly:
doppler run -- python -m src.planning simulate
```

### Inspect logs

```bash
tail -f ~/Library/Logs/com.sparkry.planning-monthly.log
tail -f ~/Library/Logs/com.sparkry.planning-monthly.error.log
```

### Reload after code change

```bash
launchctl unload ~/Library/LaunchAgents/com.sparkry.planning-monthly.plist
launchctl load ~/Library/LaunchAgents/com.sparkry.planning-monthly.plist
```

## Ad-hoc CLI

```bash
doppler run -- python -m src.planning simulate                        # full run, persist
doppler run -- python -m src.planning simulate --dry-run              # no persist
doppler run -- python -m src.planning simulate --override spend_start=300000 --note "what if?"
doppler run -- python -m src.planning simulate --scenarios baseline_ret8_horizon85
doppler run -- python -m src.planning show-latest
doppler run -- python -m src.planning compare --since 2026-01-01
```

## API

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/api/planning/runs/latest
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR: No AccountBalanceSnapshot rows` | Plaid balance sync hasn't run | `doppler run -- python scripts/plaid_balance_sync.py` first |
| `WARNING: latest AccountBalanceSnapshot is N days old` | Plaid balance sync stale | Same — re-run sync, then re-invoke planning |
| `unknown override key: X` | Typo in `--override KEY=val` | CLI lists valid keys in the error |
```

- [ ] **Step 4: Commit**

```bash
git add com.sparkry.planning-monthly.plist docs/operational/planning-engine-ops.md src/planning/scheduler.py
git commit -m "feat(planning): monthly launchd plist + ops docs (REQ-PLAN-012)"
```

---

## Task 15: End-to-end smoke test

Last task — exercises the full path: fixture DB → CLI simulate → DB row → API → returned JSON. If this passes, sub-project #1 is functionally complete.

**Files:**
- Create: `src/planning/test_e2e.py`

- [ ] **Step 1: Write the smoke test**

```python
"""End-to-end smoke: fixture DB → CLI simulate (persist) → API → JSON."""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import get_db
from src.api.main import app
from src.db.connection import Base
from src.planning.cli import main as cli_main

FIXTURE_DB = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "planning" / "accounting.fixture.db"


def test_e2e_simulate_then_api(tmp_path: Path) -> None:
    """Full pipeline: persist a run via CLI, fetch it back via API."""
    db_path = tmp_path / "e2e.db"
    shutil.copy(FIXTURE_DB, db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)

    # Step A: CLI persists a run
    with patch("src.planning.cli._open_session") as open_sess:
        open_sess.return_value = Sess()
        rc = cli_main(["simulate", "--n-sims", "300", "--note", "e2e"])
        assert rc == 0

    # Step B: API reads it back
    def override_get_db():
        s = Sess()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        tc = TestClient(app)
        resp = tc.get("/api/planning/runs/latest", headers={"X-API-Key": "test"})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["notes"] == "e2e"
        assert payload["source"] == "cli"
        assert len(payload["scenarios"]) == 15
        # Spot-check one scenario shape
        baseline = payload["scenarios"]["baseline_ret8_horizon85"]
        assert 0.0 <= baseline["survival"] <= 1.0
        assert "percentiles" in baseline
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: Run the smoke test**

Run: `pytest src/planning/test_e2e.py -v`
Expected: PASS. If 401 instead of 200, see the auth note in T13 — adjust the header to match the project convention.

- [ ] **Step 3: Full project test suite**

Run: `pytest src/planning/ -v && ruff check src/planning/ && mypy src/planning/`
Expected: All planning tests PASS, no ruff/mypy errors.

- [ ] **Step 4: Final commit**

```bash
git add src/planning/test_e2e.py
git commit -m "test(planning): end-to-end smoke — CLI persist → API roundtrip"
```

---

## Definition of Done

All boxes below MUST be checked before marking sub-project #1 complete. If any fail, fix and re-run before declaring done.

- [ ] All 17 REQ-PLAN-NNN have at least one test that fails when the requirement is violated.
- [ ] `pytest src/planning/ -v` — all tests pass.
- [ ] `ruff check src/planning/` — clean.
- [ ] `mypy src/planning/` — clean.
- [ ] `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` — clean round trip.
- [ ] Manual smoke: `doppler run -- python -m src.planning simulate --dry-run` runs against your real local DB without errors (or fails with REQ-PLAN-014 message if `AccountBalanceSnapshot` is empty).
- [ ] `com.sparkry.planning-monthly.plist` installed and visible in `launchctl list`.
- [ ] `docs/operational/planning-engine-ops.md` is accurate.
- [ ] Sub-project #1 PR is ready for review (or `feat/planning-engine` branch is ready to merge directly to main, per project convention).

---

## What's NOT in this sub-project (handoff to #2/#3/#4)

- **Projection-vs-reality tracking** (sub-project #2). The `PlanningRun` rows now exist; #2 builds a CLI/UI that overlays actual `AccountBalanceSnapshot` net worth on prior simulation envelopes.
- **Interactive wealth-UI scenario tab** (sub-project #3). Consumes `GET /api/planning/runs/latest`. May need a `POST /simulate` endpoint added later for slider-driven re-runs.
- **Monthly Amy-facing AI-generated check-in report** (sub-project #4). Consumes `PlanningRun` rows + the broader monthly summary (P&L, exceptions). Sends email and posts to the site.
- **Spousal-SS modeling** (sub-project 1b). Uses Amy's earnings history captured in spec §9.
- **Roadmap §9.B return-model upgrades** (Student-t, block-bootstrap, stochastic inflation). v1.1.
- **Roadmap §9.C full tax/account modeling** (three-pool, RMDs, ACA, 72(t)). Its own multi-week sub-project.
