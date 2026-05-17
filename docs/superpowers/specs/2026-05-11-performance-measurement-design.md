# Performance Measurement Design Spec

> Per-position and per-account performance views: value-over-time decomposed into principal vs growth, plus time-weighted and money-weighted returns. Builds on the brokerage Phase 3 data (HistoricalPrice, BrokerageTransaction, Position, CostBasisLot). No build now — design only.

**Status:** Design draft, not yet scheduled.
**Depends on:** Brokerage Phases 1–3 (shipped). No new external data sources required.
**Touches:** `src/models/brokerage.py`, `src/api/routes/brokerage.py`, `dashboard/src/routes/brokerage/`, one new module `src/analytics/performance.py`.

---

## 1. Goals

1. **Value over time per position** — the existing `/api/brokerage/holdings/{symbol}/history` already returns total value series. Extend it to decompose into a *principal* component and a *growth* component so the user can see "how much I put in vs how much it grew."
2. **Same decomposition per account and per portfolio** — same math at coarser granularity.
3. **Comparable return numbers** — one **time-weighted return (TWR)** for comparing to benchmarks, one **money-weighted return (XIRR)** for "how *I* did" given my contribution timing.
4. **Reinvestment-aware** — reinvested dividends/capital gains must not inflate "principal." They are internal events, not new outside money.
5. **Multi-account-aware** — internal transfers between the user's own accounts must not register as deposits/withdrawals on either side.

### Non-goals

- Sector/factor attribution. Out of scope.
- Tax-lot-level reporting (already covered by `realized-gl` and `CostBasisLot`).
- Risk metrics (Sharpe, beta, drawdown). Could be a follow-on; not in this spec.
- Currency conversion. All holdings are USD.

---

## 2. The three measurements

### 2.1 Principal vs Growth decomposition

Two valid views — the spec ships **both**, with the outside-money view as default.

**View A — Outside-money basis (default)**
- `principal(t)` = cumulative net external cash flow into the position/account up to time `t`.
- `growth(t)` = `market_value(t) − principal(t)`.
- Reinvested distributions are **not** principal; they show up as growth that compounded.
- Answers: *"how much money did I actually put in, and how much did the market give me on top of that?"*

**View B — Cost-basis basis (toggle)**
- `principal(t)` = sum of cost basis across open lots at time `t` (matches `CostBasisLot.cost_basis_total`).
- `growth(t)` = `market_value(t) − cost_basis(t)` = unrealized gain.
- Reinvested lots add to principal because each reinvestment creates a new lot with its own basis.
- Answers: *"what is my unrealized gain right now if I sold today?"* — the Schwab/Fidelity statement view.

Both views must agree on `market_value(t)`. They diverge only on what `principal` means.

### 2.2 Time-Weighted Return (TWR)

For comparing against benchmarks (S&P 500 buy-and-hold overlay already exists on net-worth-history). TWR removes the effect of contribution timing.

**Calculation:** chain-link sub-period returns between every external cash flow.

```
For each interval [t_i, t_{i+1}] bounded by external cash flows CF_i:
    r_i = (V(t_{i+1}) - CF_{i+1}) / V(t_i)        # if CF occurs at end of period
TWR = ∏ r_i  − 1
```

Annualize as `(1 + TWR)^(365 / days) − 1` if reporting on a multi-year window.

### 2.3 Money-Weighted Return (XIRR)

For "how did *I* do" given when I contributed. Solves for the discount rate `r` such that:

```
Σ CF_k / (1 + r)^((t_k − t_0) / 365) + V(t_end) / (1 + r)^((t_end − t_0) / 365) = 0
```

Use `scipy.optimize.brentq` on the NPV function with bracket `[-0.99, 10.0]`. Fallback to bisection on edge cases (single deposit, no time elapsed) — return `None` rather than blow up.

### 2.4 Display surface

| Surface | Default view |
|---|---|
| Holding page (`/brokerage/holdings/{symbol}`) | Stacked area: principal (bottom), growth band (top). Stat strip: TWR, XIRR, S&P TWR over same window. |
| Account detail (`/brokerage/accounts/{id}`) | Same stacked area + stat strip aggregated across all positions in account. |
| Net-worth page (already exists) | Add a "principal vs growth" toggle on the existing chart. |

Toggle between View A and View B is a single button at chart corner — remembers selection in localStorage.

---

## 3. The core modeling decision: classifying every transaction's cash-flow type

This is the **hard part**. The math is mechanical once classification is right.

### 3.1 Classification target

Every `BrokerageTransaction` gets a derived `cash_flow_type` ∈ `{external_in, external_out, internal, none}`:

| Type | Meaning | Examples |
|---|---|---|
| `external_in` | Outside money entering the portfolio | Wire/ACH deposit, payroll contribution, employer match, RSU vest (when initially deposited as shares — gross value at vest) |
| `external_out` | Money leaving the portfolio to outside | Withdrawal, distribution paid out to bank, RMD |
| `internal` | Movement between user's own accounts/positions; no outside money involved | ACAT, journal between own accounts, reinvested dividend (cash → shares), exchange (mutual-fund swap), stock split |
| `none` | Already captured by price; not a cash flow | Buy/sell within an account (cash and shares both internal), valuation adjustment, fee debited from sweep |

**Critical point on reinvestments:** A reinvested dividend is *two* events — the dividend (income to the account) and the buy (cash → shares). Both are `internal` from the **portfolio's** perspective: no outside money moved. The dividend is growth that got recycled into shares.

From the **position's** perspective (when scoping to one symbol), a reinvested-dividend buy *is* an `external_in` for that symbol — cash from the account's sweep was used to buy shares of that symbol. This is the reason View A's "growth" at the *position* level can look smaller than at the *account* level: at the position level the reinvested cash counts as principal in, at the account level it doesn't.

The spec accepts this asymmetry. The principal/growth lines are scope-relative.

### 3.2 How to classify

Map from `CanonicalAction` (already exists, `src/models/enums.py:300`):

| CanonicalAction | Portfolio scope | Account scope | Position scope |
|---|---|---|---|
| `BUY` | `none` (cash→shares within account) | `none` | `external_in` (cash entering this symbol) |
| `SELL` | `none` | `none` | `external_out` |
| `DIVIDEND_*`, `INTEREST`, `CAPITAL_GAIN_*` | `none` (income produced *by* holdings, not a deposit) | `none` | n/a (cash, not a symbol position) |
| `REINVEST` | `none` | `none` | `external_in` for the symbol |
| `RSU_VEST` | `external_in` (gross FMV at vest) | `external_in` | `external_in` for the symbol |
| `CONTRIBUTION` | `external_in` | `external_in` | n/a (lands as cash; later `BUY` allocates) |
| `DISTRIBUTION` | `external_out` | `external_out` | n/a |
| `TRANSFER`, `JOURNAL`, `EXCHANGE` | **needs pairing — see §3.3** | depends on pair | depends on pair |
| `STOCK_SPLIT`, `CASH_IN_LIEU`, `SWEEP`, `FEE`, `VALUATION_ADJUSTMENT`, `OTHER` | `none` | `none` | `none` |

### 3.3 Pairing transfers between own accounts

A transfer is `external` only if the other side is *not* an account in the user's `Account` table. The system already has `BrokerageTransaction.paired_transaction_id` (`src/models/brokerage.py:187`) for this. Pairing logic:

1. If `paired_transaction_id` is set → both sides are internal; classify as `internal`.
2. Else attempt auto-pair on `(date ± 5 business days, abs(amount) within $0.01, opposite signs, different account_id)`. Confidence threshold; on tie, leave unpaired.
3. Unpaired transfers default to `external`. Surface in `/brokerage/data-integrity` as "unpaired transfers" so the user can resolve from the UI.

The user can also manually pair from the account detail page (new affordance) — endpoint `POST /api/brokerage/transactions/{id}/pair` with body `{"paired_transaction_id": "..."}`.

### 3.4 RSU vests

RSU vests are the one source of true outside money that doesn't look like an ACH. Treat the vest as `external_in` at gross FMV (qty × close price on vest date). The tax-withholding sell-to-cover (typically same day) is a `SELL` with no special handling — it just shows up as growth being immediately realized.

---

## 4. Data model changes

Minimal — most data exists. Two additions:

### 4.1 New column: `BrokerageTransaction.cash_flow_type`

```python
cash_flow_type: Mapped[str] = mapped_column(
    String(16),
    nullable=False,
    server_default="none",
    comment="Derived: external_in | external_out | internal | none. "
            "Computed at ingest; recomputed on pairing changes.",
)
```

CHECK constraint on the four enum values. New enum `CashFlowType(StrEnum)` in `src/models/enums.py`.

**Why store it instead of deriving on read?** Pairing decisions are stateful (user can manually pair later) and the classification is hot — every performance query reads it. Storing + recomputing on pair-mutation keeps the query path O(N) without joins.

### 4.2 New table: `DailyPositionValue` (optional, performance-only)

Materializing the daily `(account_id, symbol) → value` series speeds up the chart endpoints. The data is fully derivable from `Position` snapshots + `HistoricalPrice` + transactions, so this is a cache:

```python
class DailyPositionValue(Base):
    __tablename__ = "daily_position_value"
    account_id: PK
    symbol: PK
    date: PK
    quantity: Numeric(18, 8)
    market_value: Numeric(18, 4)
    cost_basis: Numeric(18, 4)   # for View B
    principal_external: Numeric(18, 4)  # cumulative external_in − external_out, position scope
    # No index on date alone — composite PK covers the query patterns.
```

Refreshed nightly by a new job `scripts/refresh_position_values.py`. Initial implementation can skip this table and compute on-the-fly; add it when latency on the holding page exceeds ~500ms.

### 4.3 No changes needed to

- `Position`, `HistoricalPrice`, `CostBasisLot`, `Account` — all already sufficient.
- `Transaction` (the main accounting register) — out of scope; this is brokerage-only.

---

## 5. API surface

All new endpoints under `/api/brokerage/`:

### 5.1 `GET /performance/holding/{symbol}`

```
Query:  start_date, end_date, account_ids?[], view=outside_money|cost_basis
Returns:
{
  "symbol": "VTI",
  "view": "outside_money",
  "series": [
    {"date": "2024-01-01", "market_value": 12500.00, "principal": 10000.00, "growth": 2500.00},
    ...
  ],
  "summary": {
    "twr": 0.1842,           // 18.42% over the window
    "twr_annualized": 0.1235,
    "xirr": 0.1567,
    "benchmark_twr": 0.1102,  // S&P over same window
    "current_value": 14250.00,
    "total_principal": 10000.00,
    "total_growth": 4250.00
  }
}
```

### 5.2 `GET /performance/account/{account_id}`

Same shape, scoped to one account. `series` rows include all positions aggregated.

### 5.3 `GET /performance/portfolio`

Same shape, all accounts (filterable by tags via existing tag-filter mechanism).

### 5.4 `POST /transactions/{id}/pair`

Body: `{"paired_transaction_id": "..."}`. Sets `paired_transaction_id` on both sides, recomputes `cash_flow_type` for both, returns updated rows. Idempotent.

### 5.5 `GET /performance/unpaired-transfers`

Lists candidate-but-unconfirmed transfers for the user to resolve. Drives a panel on `/brokerage/data-integrity`.

---

## 6. Computation modules

New module `src/analytics/performance.py`:

```python
def principal_growth_series(
    session: Session,
    scope: PerformanceScope,   # portfolio | account_id | (account_id?, symbol)
    start: date,
    end: date,
    view: Literal["outside_money", "cost_basis"],
) -> list[DailyPoint]: ...

def time_weighted_return(
    daily_values: list[DailyPoint],
    cash_flows: list[CashFlow],
) -> Decimal: ...

def money_weighted_return(
    cash_flows: list[CashFlow],
    terminal_value: Decimal,
    terminal_date: date,
) -> Decimal | None: ...
```

Co-located test `src/analytics/test_performance.py`. Tests reference REQs (new REQ-PERF-001..00n added to `requirements/current.md`).

### Edge cases that must have explicit tests

1. **Empty position** (sold all shares mid-window) — XIRR has terminal value 0, TWR period ends at last sale.
2. **Single deposit, no time elapsed** — return `None` for XIRR; TWR = 0.
3. **Position opened mid-window** — start the series at first transaction, not `start`.
4. **Negative XIRR** (lost money) — bracket must include negative rates; `brentq` with `[-0.99, 10.0]`.
5. **Stock split** — quantity changes, price per share changes; market value continuous; not a cash flow.
6. **Reinvested dividend at account scope** — does NOT increment principal. At position scope, it DOES increment principal (for that symbol).
7. **Internal transfer mid-window** — both sides classified `internal`; portfolio principal unchanged; per-account principal changes equal-and-opposite.
8. **Unpaired transfer** — classified `external_*` by default; principal line shows a step; user can pair retroactively to flatten.
9. **RSU vest** — `external_in` at gross FMV; same-day sell-to-cover is just a `SELL`, lowers position quantity.
10. **Window boundary cash flow** — flow on exactly `start` is included; flow on `end` is included; documented in module docstring.

---

## 7. UI changes

### 7.1 Holding page (`dashboard/src/routes/brokerage/holdings/[symbol]/+page.svelte`)

- Existing line chart of value-over-time gets a new "decomposed" toggle.
- When toggled: replaces line with a stacked area (principal bottom, growth top). Hover tooltip shows all three numbers.
- New stat strip above chart: `TWR`, `XIRR`, `S&P TWR` (small label clarifying time-window).
- View toggle (outside-money / cost-basis) as a small segmented control at chart corner.

### 7.2 Account detail page

Same components as holding page, scoped to account.

### 7.3 Net-worth page

Existing chart adds the principal/growth toggle. S&P overlay stays.

### 7.4 Data-integrity panel

New section: "Unpaired transfers (N)". Click → modal with pairing UI (select counterparty transaction, confirm).

---

## 8. Build sequence (when scheduled)

Roughly 1–1.5 weeks of focused work split as:

1. **Classification** (~2 days)
   - Add `CashFlowType` enum, `BrokerageTransaction.cash_flow_type` column, Alembic migration.
   - Classification function + tests for every `CanonicalAction`.
   - Backfill script: classify every existing row.
   - Auto-pair pass over existing transfers.

2. **Computation module** (~2 days)
   - `principal_growth_series`, `time_weighted_return`, `money_weighted_return`.
   - Full unit-test matrix from §6.

3. **API endpoints** (~1 day)
   - Three `/performance/...` endpoints + pairing endpoint + unpaired-transfers endpoint.
   - Integration tests against a seeded fixture.

4. **UI** (~2–3 days)
   - Stacked-area chart component (reusable across holding/account/portfolio pages).
   - View toggle + stat strip.
   - Pairing modal on data-integrity page.

5. **Optional caching** (~1 day, defer until measured)
   - `daily_position_value` table + nightly refresh job.

### Quality gates (per CLAUDE.md)

- TDD: tests first, REQ-IDs referenced.
- `pytest && ruff check && mypy` clean.
- Run `/qpipeline thorough` for the whole feature — review-loop to zero P0/P1 across security/financial-correctness/code-quality/test-coverage.

---

## 9. Open questions to resolve before build

1. **RSU vest valuation source.** Gross FMV at vest = `quantity × close_price_on_vest_date`. Do we trust the broker's reported value, or recompute from `HistoricalPrice`? Recommend: recompute, and store the broker-reported value in `raw_data` for cross-check. Surface mismatches > 1% in data-integrity.
2. **Window-edge cash flows for TWR.** Convention: cash flow on a date is treated as **end-of-day** for that date. Documented in `performance.py` docstring; tested explicitly.
3. **Sub-day ordering** when multiple transactions occur on the same date (e.g., dividend + same-day reinvest). Convention: order by `CanonicalAction` enum order, then by `paired_transaction_id` to keep paired sides adjacent. Probably fine; revisit only if it changes a return number meaningfully.
4. **Annualization floor.** For windows < 30 days, do not annualize — show period return only, with a label like "30-day return." Annualizing a 5-day return is misleading.
5. **Pre-system-start positions.** Positions that pre-date the user's transaction history (imported as opening balance via XLSX) — what's their "principal"? Recommend: treat the opening balance snapshot as a single synthetic `external_in` at that date for the synthetic-share quantity × that-date close. Mark it visibly in the UI so the user knows the pre-system performance is unknowable. Could also be a follow-on enhancement once cost-basis lot history is fuller.

---

## 10. Out of scope (parking lot)

- Sharpe / Sortino / max drawdown.
- Per-tax-lot return contribution.
- After-tax return (would need integration with the tax-export module).
- Multi-currency.
- Benchmark choice beyond S&P 500 (no NASDAQ, VT, custom blends).
- Goal-based / target-date forecasting.

Revisit any of these as separate specs if/when needed.
