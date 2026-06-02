# Retirement & Business Sustainability Planning Engine — Design (v1)

> Sub-project #1 of integrating the Sparks Retirement & Business Sustainability Model (source: `~/Downloads/retirement_business_model_SPEC.md`, "v1 planning spec") into the accounting/tax/wealth system. Ports the Monte Carlo engine, wires it to live data, and persists each run for downstream consumers.

---

## 1. Context

The source spec is a Monte Carlo planning model already validated in a planning conversation: it answers "will the money last?" across return regimes, business-income scenarios, and SBLOC strategies. It currently lives as a Python reference implementation (source spec Appendix A) with a hardcoded `PARAMS` dict.

The user wants to incorporate it into this repo so that:
- Inputs auto-pull from live data (wealth net worth, register spend, Sparkry P&L) instead of being re-typed.
- Each run is persisted and comparable over time (projection vs. reality).
- A future wealth-UI tab lets him and Amy slide scenarios interactively.
- A future monthly AI-generated check-in report for Amy can incorporate current survival numbers.
- Claude Code can run ad-hoc analyses against the engine + persisted history.

### Decomposition (out of scope for this spec)

This is sub-project #1 of four. The others are scoped separately and depend on #1's persisted output:

| # | Sub-project | Status |
|---|---|---|
| 1 | **Planning engine + live inputs (this spec)** | designing |
| 2 | Projection-vs-reality tracking (overlay actual `AccountBalanceSnapshot` net worth on prior simulation envelopes) | deferred |
| 3 | Interactive wealth-UI scenario tab (`/wealth/planning` on Cloudflare) | deferred |
| 4 | Monthly Amy-facing AI-generated check-in report (broader than retirement model) | deferred |

A fifth idea — a dedicated Claude Code MCP/tool surface for ad-hoc analysis — is deferred indefinitely; once #1's function API exists, ad-hoc analysis is "Claude reads the code and runs it."

---

## 2. Scope

### In scope (v1)

- Port the Monte Carlo recursion from the source spec's Appendix A into `src/planning/engine.py` as a vectorized NumPy implementation.
- Extend to a **two-pool** model (taxable + retirement) with a 59.5 access constraint, replacing the source spec's single-pool / single-`tax_gross` fudge.
- Model **household income** as two separate streams — Sparkry consulting (`biz_income`/`biz_years`) and Amy's W-2 wages (`amy_wage_income`/`amy_wage_years`) — both offsetting draw while active.
- Load live inputs from the existing local SQLite (wealth `AccountBalanceSnapshot`, `Transaction` register) without modifying their schemas.
- Run a default **scenario grid** (15 cells) reproducing source-spec §7 and adding the most-used what-ifs.
- Persist each invocation as a single `PlanningRun` row.
- Expose one read-only API endpoint (`GET /api/planning/runs/latest`) for future UI / report consumption.
- Run automatically on the 1st of each month via a new `launchd` service (`com.sparkry.planning-monthly.plist`).
- CLI: `simulate`, `show-latest`, `compare`.
- Regression tests reproducing source-spec §7 results (±1pp survival).

### Explicitly out of scope (v1)

- **Return-model upgrades** (Roadmap §9.B in the source spec): Student-t fat tails, historical block-bootstrap, stochastic correlated inflation, stock/bond glidepath. Defer to v1.1 once we've seen what Normal returns get wrong against actuals.
- **Full tax/account modeling** (Roadmap §9.C): three-pool (taxable/traditional/Roth), LTCG vs ordinary, RMDs at 73/75, ACA pre-65 subsidies, 72(t)/SEPP, withdrawal-order optimization, WA cap-gains threshold logic. v1 keeps a simple two-pool + per-pool `tax_gross`; the rest is its own multi-week sub-project.
- **SBLOC mechanics** (Roadmap §9.D): collateral value, advance rate, maintenance threshold, forced-liquidation modeling, concentration haircuts. v1 supports `loan_mode` from the source spec (`interest_only` / `amortize10`) as a fixed flow, no margin-call simulation.
- **Stochastic business module** (Roadmap §9.E): revenue ramp paths, exit probability × multiple, QSBS trust-stacking. v1 takes `biz_income` and `exit_amount` as deterministic inputs as the source spec does.
- **Flexible spending rules** (Roadmap §9.F): Guyton-Klinger, guardrail spending, SS claiming-age optimization. v1 uses the source spec's glide.
- **Reporting / email / dashboard UI**: sub-projects #2, #3, #4.

---

## 3. Architecture

New module under `src/planning/`, organized the same way as other domain modules (`adapters/`, `invoicing/`, `tax_docs/`):

```
src/planning/
  __init__.py
  engine.py         — pure NumPy Monte Carlo. No I/O, no DB. (Params, ScenarioGrid) → Results.
  params.py         — Params dataclass + ScenarioGrid + spec defaults. Single source of truth for input shape.
  inputs.py         — Live-input loaders. Only file that queries register + wealth tables. Returns LiveInputs snapshot.
  models.py         — Single SQLAlchemy table: PlanningRun.
  cli.py            — argparse entry: simulate / show-latest / compare.
  api.py            — FastAPI route: GET /api/planning/runs/latest. Mounted from src/api/main.py.
  scheduler.py      — Thin wrapper for the monthly launchd job; calls cli.simulate(persist=True).
  test_engine.py
  test_inputs.py
  test_api.py
  test_persistence.py
```

### Boundary discipline

- **`engine.py` knows nothing about SQLite, FastAPI, or Doppler.** Pure function from `Params` → `Results`. This is what makes it testable against source-spec §7 numbers and what makes future Roadmap §9.B return-model upgrades a one-file change.
- **`inputs.py` is the only place that touches the register / wealth tables.** If those schemas move (e.g., the in-flight Hetzner accounting migration — `docs/superpowers/specs/2026-06-01-accounting-hetzner-migration-design.md`), this is the only file that changes.
- **`Params` is a frozen dataclass.** Live inputs and CLI overrides are *merges* applied through an explicit `merge_live_into(params, live_inputs, overrides)` helper, so every persisted `PlanningRun` row preserves which numbers came from spec defaults, live data, or human overrides.

---

## 4. Components

### 4.1 Two-pool engine

The source spec's recursion (steps 1–9 in its §5) is preserved exactly, with one change to step 5 (`P -= draw`):

```
While age < 59.5:
    All draws come from `P_taxable` (apply `tax_gross_taxable = 1.13`).
    If `P_taxable` falls below the draw, the simulation path is recorded as
    "ruined-early." Survival counts only paths where both pools were intact
    through the horizon.

While age >= 59.5:
    Draws split pro-rata by current balance. Gross-up applied per-pool:
    `tax_gross_taxable = 1.13`, `tax_gross_retirement = 1.25`.
    No RMDs (deferred — flagged in run output).
```

Income calculation (source-spec §5 step 3) is extended to include Amy's wage income as a separate deterministic stream:

```
income = (biz_income       if t < biz_years       else 0)
       + (amy_wage_income  if t < amy_wage_years  else 0)
       + (ss_amount * (1 + inflation)**t if age >= ss_start_age else 0)
```

`biz_income` and `amy_wage_income` are tracked as separate parameters (not summed) so each can have its own end-year, and so sub-project #4's monthly report can attribute income offsets to source. v1 defaults: `amy_wage_income=80000`, `amy_wage_years=3` (see §9 for context).

Vectorized via `np.random.default_rng` + matrix ops: the per-sim loop in the source spec's Appendix A becomes `(n_sims × yrs)` array ops, ~100× faster — lets the full 15-cell scenario grid run in under a second.

### 4.2 Live-input loaders

Concrete queries against existing tables — no schema changes:

| Input | Source table | Query |
|---|---|---|
| `pool_taxable` | `AccountBalanceSnapshot` (latest per-account) | `sum(balance)` where `account_type IN ('TAXABLE','BROKERAGE')` |
| `pool_retirement` | `AccountBalanceSnapshot` (latest per-account) | `sum(balance)` where `account_type IN ('IRA','ROTH','401K','HSA','PENSION','ANNUITY')` |
| `ttm_spend` | `Transaction` | `sum(abs(amount))` where `direction='expense' AND entity='personal' AND date >= today - 365d` |
| `ttm_biz_income` | `Transaction` | `sum(amount)` where `entity='sparkry' AND direction='income' AND date >= today - 365d` |
| `ttm_personal_income` | `Transaction` | `sum(amount)` where `entity='personal' AND direction='income' AND date >= today - 365d`. Informational only — shown alongside `amy_wage_income` so drift between Amy's planning value and her actual W-2 deposits is visible. Distinguishing Amy's W-2 from other personal income (Travis's distributions, refunds) requires classification beyond v1 scope; v1 just shows the gross. |
| `ttm_tax_effective` | tax-export trailing year | Informational only — shown alongside `tax_gross` in output; never overrides. |

**Sourcing convention** (set by user during design):
- **Pool defaults to live** ("what we have" *is* reality; there is no planning value for pool).
- **Everything else defaults to planning** values from the source spec; the live actual is shown alongside in CLI output and persisted into `live_inputs_json` for drift inspection.
- **CLI `--override` flags** trump both.

### 4.3 Scenario grid

Default 15-cell grid run on every `simulate` invocation:

```
baseline_ret{6.5,8,10}_horizon{85,90,95}           # 9 cells — source-spec §7 survival table
+_loan_1m_io                                        # adopted glide + $1M SBLOC interest-only forever
+_loan_1m_amort10                                   # adopted glide + $1M SBLOC, 10-yr amortize
+_biz_320k_10y                                      # adopted glide + biz $320k/yr × 10y, no exit
+_biz_320k_10y_qsbs_5m                              # same + $5M QSBS exit at year 10
+_biz_320k_10y_qsbs_10m                             # same + $10M QSBS exit
```

Each cell produces `{survival: float, percentiles: {age: [p10, p50, p90]}, owed: float}`. Subset selection via `--scenarios <name1>,<name2>,...`.

Scenario names are stable identifiers (used as JSON keys in `scenarios_json` and for `compare` diffs across runs).

### 4.4 `PlanningRun` schema

One row per `simulate` invocation, stored in `data/accounting.db`:

```python
class PlanningRun(Base):
    __tablename__ = "planning_runs"
    id: int                          # PK
    run_at: datetime                 # UTC, indexed
    source: str                      # 'cli' | 'scheduled' | 'api'
    params_json: dict                # full Params as committed (defaults + live merges + overrides)
    live_inputs_json: dict           # LiveInputs snapshot at run time, regardless of whether used.
                                     #   Includes pool_taxable, pool_retirement, ttm_spend,
                                     #   ttm_biz_income, ttm_tax_effective, staleness_warning.
    scenarios_json: dict             # {scenario_name: {survival, percentiles, owed}}
    notes: str | None                # optional CLI --note tag (e.g., "post-Schwab-rebalance")
    created_at: datetime             # audit-trail standard
    updated_at: datetime             # audit-trail standard
```

JSON columns (not normalized) — consumers are humans reading reports and future Claude analyses; flexibility beats query efficiency for this volume (~12 scheduled runs/year + ad-hoc). Alembic migration creates the table; no other schema touched.

### 4.5 CLI

```bash
doppler run -- python -m src.planning simulate
doppler run -- python -m src.planning simulate --dry-run                       # no persist
doppler run -- python -m src.planning simulate --override spend_start=300000   # override planning value
doppler run -- python -m src.planning simulate --scenarios baseline_ret8_horizon85,+_biz_320k_10y_qsbs_5m
doppler run -- python -m src.planning simulate --note "stress test"            # tag the run
doppler run -- python -m src.planning show-latest                              # pretty-print most recent run
doppler run -- python -m src.planning compare --since 2026-01-01               # diff survival across runs
```

`--override` accepts any key in the `Params` dataclass; unknown keys are rejected with a listing of valid keys. Doppler-wrapped invocation matches every other script in the repo.

### 4.6 API

One read-only endpoint, mounted from `src/api/main.py`:

```
GET /api/planning/runs/latest
  → 200 { id, run_at, source, params, live_inputs, scenarios, notes }
  → 404 if no runs exist
```

v1 deliberately does **not** expose `simulate` over HTTP — re-runs go through the CLI/scheduler so we don't have to think about authn or compute throttling for the wealth-UI integration yet. Sub-project #3 will revisit when the dashboard needs slider-driven re-runs.

### 4.7 Scheduler

New launchd plist `com.sparkry.planning-monthly.plist`, modeled on `com.sparkry.weekly-pl-report.plist`:

- Runs on the 1st of each month at 06:00 local time.
- Invokes `doppler run -- python -m src.planning simulate --source scheduled`.
- Failure → stderr logged like every other launchd job; the next invocation just runs.

---

## 5. Data flow

Single `simulate` invocation:

```
                                ┌─ wealth.AccountBalanceSnapshot ─┐
                                ├─ Transaction (entity=personal) ─┤
                                ├─ Transaction (entity=sparkry) ──┤
                                │                                 │
   spec defaults ──► Params ◄─── inputs.load_live() ─► LiveInputs ┘
                       │                                  │
                       ├── merge(planning, live, overrides) ──►│
                       │                                       ▼
                       ▼                          (pool from live;
                  ScenarioGrid                   other inputs from planning
                       │                          unless overridden)
                       ▼
                  engine.simulate_grid(Params, ScenarioGrid)
                       │
                       ▼
                  Results { scenario_name → {survival, percentiles, owed} }
                       │
                       ▼
                  PlanningRun(params_json, live_inputs_json, scenarios_json)
                       │
                       ▼
                  SQLite (data/accounting.db)
                       │
                       ├── GET /api/planning/runs/latest
                       ├── CLI: show-latest, compare
                       ├── (future #2) projection-vs-reality tracker
                       ├── (future #3) /wealth/planning UI
                       └── (future #4) monthly Amy-facing report
```

**Flow guarantees:**
- `Params` is built before any engine I/O. Engine never re-reads.
- `LiveInputs` is captured even when not used.
- One DB write per invocation — single `PlanningRun` row, all 15 scenarios as a JSON blob. No partial-state risk; either the whole run lands or none of it.

---

## 6. Error handling

Fail loud, fail informative — no silent fallbacks.

| Failure mode | v1 behavior |
|---|---|
| Live wealth data stale (>7 days since last `AccountBalanceSnapshot`) | Run proceeds. Warning to stderr. `live_inputs_json.staleness_warning = "last snapshot 2026-05-21, 11 days old"`. Pool still pulls live. |
| Live wealth data missing entirely (no snapshots) | Hard fail. Message: `"No AccountBalanceSnapshot rows. Run scripts/plaid_balance_sync.py, or pass --override pool_taxable=... pool_retirement=..."`. Non-zero exit. |
| `--override` for unknown key | argparse rejects with the list of valid `Params` field names. |
| `Params` defaults missing a key | TypeError at construction time. Covered by `test_params.py`. |
| DB write fails (disk full, lock contention) | Session rollback. CLI non-zero exit. Scheduled run failure surfaces in launchd stderr log. |
| Engine produces non-finite values (e.g., pathological override produces NaN) | Engine asserts `np.isfinite(paths).all()` post-sim. Failure → AssertionError with which scenario/params triggered it. |
| Stale `ttm_tax_effective` (no tax export run this year) | Informational field omitted from `live_inputs_json`; run proceeds. |

No retries — `simulate` is idempotent; re-run if it fails. Aligns with the rest of the repo's per-row error isolation philosophy without applying it here (a planning run is one atomic unit, not a batch of records).

---

## 7. Testing

Anchored on source-spec validation; same quality gates as the rest of the repo (`pytest && ruff check src/ && mypy src/`).

1. **Regression — source-spec §7 reproduction.** Lock the single-pool engine variant against the published table (flat 4%, adopted glide, with/without loan, with biz+QSBS) — survival % within ±1pp. The "model still works" canary on every CI run. *(Roadmap §9.H, built in day one.)*
2. **Two-pool unit tests.** Pre-59.5 drain-to-zero → ruin recording; pro-rata draw split post-59.5; per-pool gross-up math; access constraint boundary at exactly age 59.5.
3. **Live-input loaders against fixture DB.** `tests/fixtures/planning/accounting.fixture.db` with 3 accounts (1 taxable, 1 IRA, 1 personal checking) + 12 months of synthetic transactions; loaders return expected sums to the penny.
4. **`merge_live_into` logic.** Pool live; non-pool planning; override beats both; unknown override rejected.
5. **Persistence round-trip.** Write a `PlanningRun`, read back, JSON columns preserve nested dict structure.
6. **API endpoint.** Empty DB → 404. Populated DB → 200 with expected JSON keys (snapshot test).
7. **CLI integration test.** `simulate --dry-run` runs end-to-end against the fixture DB; output contains all 15 scenario names; no DB row written.
8. **Determinism.** Fixed seed → byte-identical results across runs.

---

## 8. REQ-IDs

Added to `requirements/current.md` under a new `REQ-PLAN-*` block:

| REQ-ID | Requirement |
|---|---|
| REQ-PLAN-001 | Monte Carlo engine reproduces source-spec §5 recursion as a vectorized NumPy implementation. |
| REQ-PLAN-002 | Engine is pure: `(Params, ScenarioGrid) → Results`, no I/O. |
| REQ-PLAN-003 | Two-pool extension: draws taxable-only while `age < 59.5`; pro-rata by current balance while `age >= 59.5`. Per-pool `tax_gross`. |
| REQ-PLAN-004 | Path is recorded as "ruined-early" if taxable hits zero pre-59.5; survival counts only intact-through-horizon paths. |
| REQ-PLAN-005 | Live-input loaders read `AccountBalanceSnapshot` and `Transaction` without modifying their schemas. |
| REQ-PLAN-006 | Pool defaults to live; other inputs default to planning; `--override` trumps both. |
| REQ-PLAN-007 | `LiveInputs` is snapshotted into every `PlanningRun` row regardless of whether values were used. |
| REQ-PLAN-008 | Default scenario grid contains the 15 scenarios listed in §4.3 and reproduces source-spec §7. |
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

---

## 9. Open questions & future work

- **Source-spec discrepancy on `tax_gross_retirement`.** v1 picks 1.25 as a placeholder for retirement-account draws (federal ordinary income, no WA tax, ~22-24% effective). Confirm with CPA before sub-project #4 publishes a number publicly; for v1 ad-hoc/internal use, the placeholder is documented and not load-bearing.
- **Whose age?** Source spec uses Travis's age (49). Amy's age affects SS claiming and survival horizon. v1 uses a single-person model from the source spec for the age timeline; couples-spousal-SS modeling is its own future sub-project (call it 1b).

- **Amy's earnings history (input to 1b's spousal-SS model).** Captured during v1 design so it's ready when 1b lands:
  - **Currently:** private-school teacher since 2018, ~$80,000/yr W-2. Plans to continue "a few more years" (v1 default: 3 yrs through ~2029).
  - **Pre-current:** stay-at-home mom 2009–2018 (counts as $0 in SS's 35-year average).
  - **Curves:** Feb 2009 end-date back to ~2004.
  - **Public-school teacher (VA, NC):** through 2004. *Pension-system question:* if either state's teacher pension paid into Social Security, those years contribute to her SS record normally; if she was in a non-SS pension system (some districts opt out), WEP/GPO may reduce her benefit. **Confirm in 1b** via her SSA Statement (ssa.gov) before computing spousal-vs-own benefit.
  - Today's `ss_amount=$50,000 household net` is presumably a rough sum of (Travis's full benefit + Amy's spousal/own benefit). 1b should decompose and validate.
- **`pool_retirement` typing.** The source spec calls out the retirement sub-pool ($1.5M) as "treated as part of the single pool." v1's two-pool extension partially addresses this. The remaining gap (Roth vs traditional vs HSA tax treatment) is Roadmap §9.C.
- **Reproducibility under engine upgrades.** When v1.1 adds Student-t, the §7 regression must remain on the Normal path. Add a `return_model='normal'|'student_t'` param; default stays `normal` until validation indicates otherwise.
