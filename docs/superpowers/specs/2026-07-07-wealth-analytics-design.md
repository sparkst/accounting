# Wealth Analytics Remediation + Policy Features — Design Spec

**Date:** 2026-07-07
**Author:** Travis Sparks (with Claude Code)
**Status:** Approved design → ready for implementation plan
**Scope:** Program 2026-07 wealth slice — REQ-FIX-WLT-001..009, REQ-FIX-DAT-001..003 (remediation), REQ-IPD-001..004, REQ-NWA-001, REQ-BBT-001..002 (features). All work is local (Hetzner/FastAPI/SQLite + SvelteKit dashboard); **no D1/Cloudflare changes in v1** except the shared parity fixture (§4).
**Branch:** `feat/remediation-and-features-2026-07`

---

## 1. Total-return price series (REQ-FIX-WLT-001)

### 1.1 Verified bug
`src/adapters/yfinance_prices.py:157,175` calls `yf.download(..., auto_adjust=False)` and drops `Adj Close`; `historical_price.close` is the **unadjusted** close and nothing downstream re-adjusts. Consequences:
- `_benchmark_twr` (`src/api/routes/brokerage.py:1874-1913`) and the buy-and-hold simulation (`:1404-1424`) compute **price-return-only** benchmark performance, while portfolio TWR includes dividend cash flows → systematically flatters the portfolio ~1.3–1.9%/yr vs SPY.
- Splits: a raw-close series steps down 2:1 on split day while snapshot quantities don't, creating value cliffs in re-priced history (§2).

### 1.2 Design
**Add `adj_close` alongside raw `close`; never replace raw close.**

- **Schema:** `historical_price.adj_close Numeric(18,8) NULL` (additive). Nullable because backfill is asynchronous and some sources (XLSX seed) have no adjusted series.
- **Adapter:** `fetch_eod` keeps `auto_adjust=False` (the frame already contains both `Close` and `Adj Close`); `HistoricalPriceRow` gains `adj_close: Decimal | None` populated via the existing `_to_decimal` path. Docstring at line 11 rewritten.
- **Backfill:** `scripts/backfill_adjusted_closes.py` (DRY-RUN default, `--apply`): for every distinct symbol in `historical_price`, fetch the full-range series and `UPDATE adj_close` on matching `(symbol, trade_date)`. `adj_close` is a **derived analytics column, not audit data** — Yahoo restates adjusted closes after every new dividend/split, so idempotent re-runs that overwrite `adj_close` are sanctioned (raw `close`, `raw`-sourced fields, and `ingested_at` are never touched). The nightly price job (`scripts/backfill_historical_prices.py`) writes `adj_close` for new rows go-forward and refreshes the trailing 30 days of `adj_close` per symbol (captures ex-div restatements without full re-pulls).

### 1.3 Which computation uses which series
| Computation | Series | Rationale |
|---|---|---|
| `_benchmark_twr` endpoints (start/end lookup) | `adj_close` | Total return, comparable to portfolio TWR (which sees dividends as internal cash) |
| Buy-and-hold benchmark sim (`_build_price_lookup` for the bench symbol) | `adj_close` | Simulated shares implicitly reinvest distributions |
| Live re-pricing of held positions (`_per_account_value_at`, `_price_at_or_before` in `src/reports/brokerage_summary.py:550-558`) | raw `close` × split factor (§2) | Market value of actually-held shares; dividends are cash, not price |
| REQ-WD-011 gap-fill reprice (D1) | unchanged in v1 | D1 out of scope; parity follow-up REQ noted in traceability |

**Fallback contract:** where `adj_close IS NULL` for an endpoint date, benchmark math falls back to raw `close` and the API response carries `benchmark_basis: "price_return"` (vs `"total_return"`); the dashboard renders a small "(price return)" caveat. No silent mixing: both endpoints of a single TWR window must use the same basis, else return `None`.

### 1.4 Migration
Alembic revision `wa2607a_adjclose_splits` (additive, real downgrade):
- `op.add_column("historical_price", sa.Column("adj_close", sa.Numeric(18, 8), nullable=True))`
- new table `stock_split` (§2)
- `downgrade()`: drop `stock_split`, then `batch_alter_table("historical_price").drop_column("adj_close")` (SQLite-safe batch mode). No data loss on downgrade beyond the derived column — acceptable and documented in the migration docstring. Validate with the `alembic-migration` skill checklist.

---

## 2. Split-safe re-pricing (REQ-FIX-WLT-002)

The re-pricing bug: value(T) = quantity(S) × close(T) with S = snapshot date. A split ex-date in (S, T] halves/doubles `close` but not the stored quantity → cliff.

**Rule:** `value(T) = quantity(S) × Π(split_ratio for ex_date ∈ (S, T]) × close(T)`, all Decimal, quantized at 2 only at the final sum.

- **`stock_split` table** (same migration as §1.4): `symbol String(32)`, `ex_date Date`, `ratio Numeric(12,6)` (post/pre, e.g. 2:1 → `2.000000`), `source String(16) default "yfinance"`, `ingested_at`; PK `(symbol, ex_date)`. Populated by `backfill_adjusted_closes.py --apply` from `yf.Ticker(sym).splits` and refreshed by the nightly job.
- **Do not derive splits from `close/adj_close` ratios** — that ratio also embeds dividends and cannot be separated.
- `_load_history_state` gains `splits_by_symbol: dict[str, list[tuple[date, Decimal]]]`; the re-price branch in `_per_account_value_at` applies the cumulative ratio. When the splits table has no rows for a symbol the ratio is `1` (today's behavior) — correctness improves monotonically as split data lands.
- Same rule applies to the holdings-history forward-fill (§5) wherever quantity×price appears.

---

## 3. E*TRADE `as_of` / hash / cost_basis (REQ-FIX-WLT-003)

### 3.1 Verified bugs (`src/adapters/etrade_csv.py`)
- `:355` — `as_of = datetime.now(UTC)` (import time, not statement time).
- `:388-396` — `compute_position_row_hash(..., as_of_iso="")`: `as_of` deliberately excluded, so a **fresh export can never write a fresh snapshot** — position history for E*TRADE is frozen at first import.
- `:420` — `cost_basis=None` always, while `avg_cost_basis` is populated → holdings views compute unrealized gain against 0 basis (phantom ~100% gains).

### 3.2 Fixes
- **`as_of` derivation (priority):** (1) `--as-of YYYY-MM-DD` CLI override; (2) the `"Generated at May 4 2026 02:47 PM ET"` footer row already recognized by `_TICKER_RE` skip logic at `:375-379` — parse it instead of discarding it; (3) file mtime (UTC date). Provenance recorded in `raw_data["as_of_source"] ∈ {"cli","embedded","mtime"}` on new rows.
- **Hash:** pass `as_of_iso=as_of.date().isoformat()` (date-quantized — intra-day re-imports of the same export stay idempotent; a new export on a new date writes new rows). This changes the hash universe: old rows keep old hashes, new imports insert alongside them — exactly the supersede-by-newer-snapshot semantics the rest of the pipeline uses (`_latest_at_or_before` picks the newest ≤ target).
- **Cost basis:** `cost_basis = (avg_cost × quantity).quantize(Decimal("0.01"))` when both present, else `None`.

### 3.3 Existing frozen snapshots — annotate + derived-field backfill (no deletes)
Decision: **annotate, not supersede.**
- `as_of` on legacy rows is left untouched. Rewriting dates would fabricate history we don't have; the import date is at least the date the data was downloaded, and the newest post-fix import naturally becomes the current snapshot.
- `cost_basis` on legacy E*TRADE rows **is backfilled in place** where `cost_basis IS NULL AND avg_cost_basis IS NOT NULL AND quantity IS NOT NULL` — it is a pure derivation of already-stored fields, not new data, so an audited UPDATE is safe. One-shot script `scripts/backfill_etrade_cost_basis.py` (DRY-RUN default) writes an `AuditEvent` per row (`field_changed="cost_basis"`, `changed_by="script:etrade_cost_basis_backfill"`). `raw_data` is never mutated.
- Justification vs supersede: superseding would duplicate every legacy row with a synthetic twin and force downstream "which twin wins" logic; annotation keeps one row per real observation and fixes only the derived field.

---

## 4. Per-name cutoff parity for networth-history dedup (REQ-FIX-WLT-004)

### 4.1 Verified divergence
Local tier-2 (`src/api/routes/brokerage.py:961-994`) suppresses **all** unmatched legacy rows at/after a single **global** earliest-matched-PositionSnapshot date. The sparkry-crm D1 port implements the correct **per-name effective cutoff** per REQ-WD-009 (requirements/current.md ~line 309-317). The two "mirror exactly" implementations have diverged: locally, a legacy name whose live counterpart onboarded late is wrongly zeroed as of the *first* account's cutover.

### 4.2 Algorithm (port the REQ-WD-009 acceptance verbatim)
For each `raw_account_name.lower()` the effective cutoff is the **earlier** of:
- **Tier 1** `matched_name_first_date[name]` — first date a matched `AccountBalanceSnapshot` carries the same raw name (already built at `:970-978`); and
- **Tier 2** `alias_cutoff_by_raw_name[name]` — per-name first matched date via an explicit legacy-name→account alias map (replacing the global `_earliest_matched_position_date`): cutoff = earliest `PositionSnapshot.as_of` **of the aliased account only**.

Absent map entry = +∞ for that tier; both absent → no cutoff, full history included. Contribution rule: include strictly **before** cutoff, exclude (contribute $0, including carry-forward) at/after. Key-casing contract per REQ-WD-009 P1-B: every map keyed on lowercased raw name.

- **New table `account_alias`** (additive migration `wa2607b_account_alias`, mirrors the D1 schema): `raw_account_name String(255)` (stored lowercased, PK), `account_id FK account.id NOT NULL`, `created_at`. Seeded by `scripts/seed_account_aliases.py` from the D1 `account_alias` export (checked-in seed JSON; DRY-RUN default). Real downgrade drops the table.
- Extract the predicate into `src/utils/networth_dedup.py::unmatched_active_at(raw_name, target, matched_first, alias_cutoff)` so it is unit-testable in isolation and structurally comparable to the TS `unmatchedActiveAt`.

### 4.3 Shared parity fixture
`tests/fixtures/wealth-parity/networth_dedup_cases.json` — a table of cases `{matched_first_dates, alias_cutoffs, unmatched_series, target_date, expected_contribution}` covering: both/one/neither cutoff, mixed-case alias vs snapshot casing, carry-forward-cut-at-cutoff, and the pre-2022 restore scenario. Consumed by pytest here and (follow-up PR to `sparkry-crm`) by vitest against `unmatchedActiveAt` — the JSON file is the contract; each repo's test asserts a checked-in SHA-256 of the fixture so silent drift fails CI in both. Also: regression test asserting today's total is unchanged vs a baseline captured pre-change (recorded figure, not hardcoded), per REQ-WD-009. CLAUDE.md "mirrors exactly" sentence updated once green.

---

## 5. Holdings history forward-fill (REQ-FIX-WLT-005)

`holding_history` (`brokerage.py:1519-1563`) groups by **exact snapshot date across accounts**: brokers snapshot on different days, so each date bucket holds only the accounts that happened to report that day → sawtooth series, and `current_*` reflects only the single most-recent date bucket (usually one account).

**Fix:** group per `(account_id)` first; build each account's ascending series; over the sorted union of all dates, carry each account's last-known `{market_value, quantity, cost_basis}` forward (split-ratio-adjusted quantity when re-priced, §2) and sum per date. `current_value/current_quantity/cost_basis` = Σ over accounts of that account's **latest** snapshot values. No staleness bound here — this mirrors networth-history carry-forward semantics; the per-account `as_of` list is included in the response for transparency.

---

## 6. Benchmark simulation anchor + staleness bound (REQ-FIX-WLT-006)

`brokerage.py:1404-1424`: `shares` is computed only on the **first** iteration; if the first target date pre-dates benchmark price history (`anchor_price is None`), `shares` stays `None` forever → silent all-`None` benchmark series. Also `_latest_at_or_before` walks back unboundedly → a delisted/stale symbol flatlines.

**Fix:**
- **Anchor:** on each iteration while `shares is None`, if `port_v > 0` and a bench price ≤7 days stale exists at `d`, set `initial_portfolio = port_v`, `shares = port_v / price`. Dates before the anchor emit `benchmark_value: None`. `portfolio_pct` measures from the anchor date (documented in the response as `anchor_date`).
- **Staleness:** `_latest_at_or_before` (price variant) gains `max_staleness_days=7` (reuse `_PRICE_ROLLBACK_DAYS`, `src/reports/brokerage_summary.py:363`); beyond that, return `None` → a **gap** in the series, never a flatline. Applies to both the sim and `_benchmark_twr` endpoint lookups.

---

## 7. wealth_client error wrapping + cloud IngestionLog (REQ-FIX-WLT-007)

`src/adapters/_shared/wealth_client.py:133-136`: `httpx.post` transport errors (`httpx.TransportError`: DNS, TLS, timeout) and `response.json()` on a non-JSON 2xx body escape the `WealthClientError` hierarchy — importer callers that `except WealthClientError` crash mid-batch.

**Fix:** wrap the POST in `try/except httpx.HTTPError as exc: raise WealthTransportError(str(exc)) from exc`; wrap `.json()` in `try/except ValueError: raise WealthProtocolError(status, body[:500])`. Both are new `WealthClientError` subclasses, exported in `__all__`. **Local IngestionLog:** every cloud-mode importer run writes one local `IngestionLog` row (`source="wealth_cloud:<adapter>"`, counts from the API response, `status ∈ {success, partial, error}`, error text on failure) so cloud pushes appear in the account-detail dossier and delivery-health surfaces exactly like local imports.

---

## 8. Plaid snapshot freshness in missing-accounts (REQ-FIX-WLT-008)

`brokerage.py:1090-1116` builds `latest_by_account` from `AccountBalanceSnapshot` + `PositionSnapshot` only. Plaid-fed accounts get daily `plaid_account_balance_snapshot` rows but no ABS/PS rows → reported stale/missing forever.

**Fix:** add a third grouped `max(as_of)` query over `PlaidAccountBalanceSnapshot`, resolved to `Account.id` via the account's Plaid linkage (`plaid_item_id`/`plaid_account_id`, or the snapshot's account FK if present), feeding the same `_record()` merge. Response gains `freshness_source` per row (`positions|balances|plaid`) for the panel tooltip.

---

## 9. IUL notes merge + `--as-of` overrides (REQ-FIX-WLT-009)

`src/adapters/north_american_iul.py:136-137` does `account.notes = notes` — every import **clobbers** human-curated notes with the machine string built at `:189-195`.

**Fix:** the importer owns only a delimited machine block:
```
<human notes preserved verbatim>
--- [na_iul auto 2026-07-07] accumulation=…; premium_paid=…; cost_basis=…
```
Merge rule: if a `--- [na_iul auto` marker exists, replace from the marker to end; else append with a separating newline. Human text above the marker is never touched; pure-machine updates remain idempotent. Same helper (`src/adapters/_shared/notes_merge.py::merge_machine_block(notes, tag, body)`) is reusable by other adapters. Additionally, the mtime-defaulting adapters (`vanguard_csv`, `fg_pdf`, `nw_mutual_xlsx`) gain a `--as-of YYYY-MM-DD` CLI override mirroring §3.2's priority ladder.

---

## 10. Account data hygiene (REQ-FIX-DAT-001..003)

- **DAT-001 — Vanguard IRA correction.** Audited data migration `wa2607c_vanguard_ira_types` (or script — migration chosen so it rides `alembic upgrade head` on the box) sets, keyed on `broker='vanguard'` + exact `account_name`:
  | account_name | account_type | tax_sheltered |
  |---|---|---|
  | Amy IRA | `trad_ira` | 1 |
  | Amy Roth IRA | `roth_ira` | 1 |
  | Travis Vanguard IRA | `trad_ira` | 1 |
  | Travis Roth IRA | `roth_ira` | 1 |
  Migration fails loudly if a name matches 0 or >1 rows; writes one `AuditEvent` per changed field (`changed_by="migration:wa2607c"`). Downgrade restores the prior values (captured via SELECT at upgrade time into the audit rows; downgrade reads them back). Tax-sheltered analytics (policy panel §11, excise headroom §11.3) then classify these correctly.
- **DAT-002 — report-only.** The monthly close report flags the unnamed Vanguard taxable account (name-or-archive decision) and the $50 Fidelity TOD (closure decision) for a human; no automated mutation.
- **DAT-003 — 401k wrapper invariant.** Verify MS 401k ↔ BrokerageLink linkage (`is_plan_wrapper=1` on the wrapper, child's `parent_account_id` → wrapper). Invariant test: seed wrapper+child each with snapshots, assert `networth-history` and `_sum_per_account_filtered` count the pair **once** (wrapper excluded when the child carries the value), and that the policy panel's investable base does likewise.

---

## 11. Feature: Investment policy dashboard (REQ-IPD-001..004)

### 11.1 Config — `config/investment_policy.yaml` (single file, shared with §13)
```yaml
concentration:
  symbols: [AMZN, MSFT]
  baseline_pct: <measured at ship, ~51>   # 2026-07 baseline, pinned
  baseline_month: 2026-07
  target_pct: 35
  target_month: 2031-07                   # linear glide, 60 months
  drift_alert_threshold_pts: 3
international_target_pct_of_equity: 10
international_symbols: [ ... ]            # explicit list; no inference
cash_symbols: [ ... ]                     # sweep/MM tickers
wa_excise:                                # per tax year, updatable
  2026: { threshold: 270000, surcharge_threshold: 1000000 }
bold_bets: { cap: 20000, symbols: { ... } }   # §13
```
Loaded via a typed loader (`src/analytics/policy_config.py`), Decimal at the YAML boundary.

### 11.2 API — `GET /api/brokerage/policy`
Computed from the latest per-account positions (same inclusion rules as net worth: closed excluded, 401k wrapper counted once per §10):
- **Investable base** = Σ market value of brokerage/retirement accounts (excludes 529s and insurance-balance accounts — beneficiary money and non-tradeable CSV).
- Per-symbol concentration %, **AMZN+MSFT combined** (includes RSU accounts), **international %** of equity, **cash %**, **embedded gain** per holding = `market_value − cost_basis` (post-§3 basis fix; `null` basis → flagged, not treated as 0).
- **Glide line:** `glide(m) = baseline_pct − (baseline_pct − 35) × months_since(2026-07, m) / 60`, Decimal, clamped at 35 after 2031-07. Response: `current_pct`, `glide_pct`, `headroom_pts = glide − current`, plus the glide series for charting.
- **WA excise headroom (REQ-IPD-003):** realized **LT** gains YTD = Σ `RealizedGainLoss.lt_gain_loss` (fallback `gain_loss where term='LT'`), `closed_date` in the tax year, **taxable accounts only** (`tax_sheltered=0`, correct after §10). Headroom vs `threshold` and `surcharge_threshold` from config.

### 11.3 Svelte panel (local dashboard only, v1)
`dashboard/src/routes/wealth/` gains a Policy panel (`PolicyPanel.svelte`): stat cards (AMZN+MSFT vs glide with headroom badge, intl %, cash %, excise headroom meter), a small glide-vs-actual line chart, an embedded-gains table sorted by gain, and the bold-bets cap status (§13). Reuses existing `/wealth` fetch + formatting helpers; no D1 port.

### 11.4 Drift alert (REQ-IPD-004)
`scripts/policy_drift_dispatch.py` (DRY-RUN default; box timer monthly or piggybacked on `accounting-balance-alerts`): if `current_pct − glide_pct > 3`, POST one `info`-severity payload to the existing n8n severity webhook (`src/balance_alerts/webhook.py`), deduped to **one per calendar month** via the `alert_dispatch` ledger (key `policy_drift:<YYYY-MM>`).

---

## 12. Feature: Net-worth attribution (REQ-NWA-001)

`GET /api/brokerage/networth-attribution?start=&end=` — decompose `ΔNW = NW(end) − NW(start)` (both from the §4-corrected networth-history valuation) into:
1. **Net flows F** = Σ `BrokerageTransaction.amount` where `cash_flow_type ∈ {external_in, external_out}` and `trade_date ∈ (start, end]` (existing portfolio-scope classification; sign already positive-in/negative-out).
2. **Coverage change C** = Σ first-observed value of accounts whose earliest snapshot falls inside the window, minus Σ last value of accounts that drop out (closed/stale past end). Isolates "we started tracking X" from real gains.
3. **Market effect M** = ΔNW − F − C (residual; total-return-consistent after §§1-2 — dividends left in cash raise NW without appearing in F, correctly landing in M).
Response: the three components + ΔNW + per-component account/tx counts, all Decimal quantized 2. A `format_weekly_line()` helper renders `NW Δ $X: market $A, flows $B, coverage $C` for the WBR email (REQ-WBR-002 tie-out).

---

## 13. Feature: Bold-bets tracker (REQ-BBT-001..002)

- **Sleeve definition:** account-level via existing `AccountTag` tag `bold-bet` (whole account in the sleeve) ∪ symbol watchlist in `investment_policy.yaml` `bold_bets.symbols` (per-symbol entries carry `thesis:` and `exit:` free text — human-edited config, v1; no new table).
- **`GET /api/brokerage/bold-bets`:** per position — cost (cost_basis), value, unrealized P&L, plus realized P&L from `RealizedGainLoss` for sleeve symbols/accounts; sleeve totals and % of investable base; each row carries its thesis/exit notes.
- **Cap (REQ-BBT-002):** `bold_bets.cap` default `20000`; sleeve value > cap surfaces as a breach chip in the Policy panel (§11.3) and a line in the monthly close report with the fixed copy recommending quick-turnaround trades be housed in the Roth. **No enforcement** — display and report only.
- Dashboard: `BoldBetsCard.svelte` under the Policy panel; sleeve drill-in lists positions with thesis/exit.

---

## 14. Test strategy

TDD; tests co-located, each referencing its REQ-ID. Highlights per REQ:
- **WLT-001:** adapter test — fixture frame with both closes → `adj_close` captured; endpoint test — dividend-paying fixture where price-return ≠ total-return, assert benchmark uses adjusted and `benchmark_basis` flag on fallback. Migration up/down round-trip.
- **WLT-002:** synthetic 2:1 split between snapshot and target — repriced series is cliff-free (±ε continuity across ex-date); no-split symbols unchanged (ratio 1).
- **WLT-003:** hash includes date (same file re-import skips; next-day export inserts); footer-date parse, mtime fallback, `--as-of` priority; `cost_basis` persisted = avg×qty quantized; backfill script writes AuditEvents and is idempotent.
- **WLT-004:** table-driven pytest over the parity fixture (both/one/neither cutoff, casing, carry-forward cut); fixture SHA guard; present-day-total regression vs recorded baseline.
- **WLT-005:** two accounts snapshotting on alternating days → summed series monotone-sensible (no sawtooth); `current_*` = sum of per-account latests.
- **WLT-006:** portfolio history predating benchmark prices → anchors at first jointly-valued date, earlier points `None`; 30-day price gap → `None` gap, not flatline.
- **WLT-007:** mocked `httpx.ConnectError` and text/plain 200 → `WealthClientError` subclasses; cloud import writes IngestionLog row (success + error paths).
- **WLT-008:** account with only Plaid snapshots → not reported missing; freshness_source correct.
- **WLT-009:** human notes + repeated imports → human text intact, machine block replaced once; `--as-of` honored on the three adapters.
- **DAT-001/003:** migration test on seeded names (0-match and 2-match fail loudly); wrapper invariant test (§10).
- **IPD:** glide math golden values (baseline month, midpoint, clamp at 2031-07); excise sums exclude tax-sheltered + ST; drift dedup one-per-month; concentration includes RSU account.
- **NWA:** synthetic scenario with a deposit, a price move, and a newly-tracked account → components recover exactly; M+F+C ≡ ΔNW property test.
- **BBT:** tag∪watchlist union (no double-count when both); cap breach boundary at exactly $20k (no breach) / $20k+0.01 (breach).
Gates: `pytest && ruff check src/ && mypy src/`. Feature phases run `/qpipeline thorough`.

---

## 15. REQ traceability

| REQ | Section | Primary files |
|---|---|---|
| REQ-FIX-WLT-001 | §1 | `src/adapters/yfinance_prices.py`, `src/models/history.py`, `scripts/backfill_adjusted_closes.py`, `src/api/routes/brokerage.py` |
| REQ-FIX-WLT-002 | §2 | `src/models/history.py` (`stock_split`), `src/reports/brokerage_summary.py` |
| REQ-FIX-WLT-003 | §3 | `src/adapters/etrade_csv.py`, `scripts/backfill_etrade_cost_basis.py` |
| REQ-FIX-WLT-004 | §4 | `src/utils/networth_dedup.py`, `src/api/routes/brokerage.py:961-994`, `tests/fixtures/wealth-parity/` |
| REQ-FIX-WLT-005 | §5 | `src/api/routes/brokerage.py:1519-1563` |
| REQ-FIX-WLT-006 | §6 | `src/api/routes/brokerage.py:1404-1424`, `src/reports/brokerage_summary.py` |
| REQ-FIX-WLT-007 | §7 | `src/adapters/_shared/wealth_client.py` |
| REQ-FIX-WLT-008 | §8 | `src/api/routes/brokerage.py:1090-1116` |
| REQ-FIX-WLT-009 | §9 | `src/adapters/north_american_iul.py`, `src/adapters/_shared/notes_merge.py`, vanguard/fg/nw_mutual adapters |
| REQ-FIX-DAT-001..003 | §10 | migration `wa2607c`, `src/api/routes/brokerage.py` invariant tests, close report |
| REQ-IPD-001..004 | §11 | `src/analytics/policy_config.py`, `/api/brokerage/policy`, `dashboard/.../PolicyPanel.svelte`, `scripts/policy_drift_dispatch.py` |
| REQ-NWA-001 | §12 | `/api/brokerage/networth-attribution`, WBR line helper |
| REQ-BBT-001..002 | §13 | `/api/brokerage/bold-bets`, `AccountTag`, `investment_policy.yaml`, `BoldBetsCard.svelte` |

## 16. Constraints & non-goals

- **Decimal everywhere** — `Decimal(str(x))` at every float/JSON/YAML boundary; quantize only at final presentation sums.
- **Migrations are additive with real downgrades**; no drops of audit columns; data migrations write AuditEvents and fail loudly on ambiguity.
- **No deletes** — E*TRADE legacy rows annotated/backfilled, never removed; `raw_data` immutable.
- **DRY-RUN default** on every new script; `--apply` opt-in.
- **Local-only v1** for all dashboard/feature surfaces (no D1/Pages changes); the parity fixture (§4.3) is the sole cross-repo artifact, applied to `sparkry-crm` in a follow-up PR. D1-side gap-fill reprice adoption of split/adj data is a named follow-up, not in scope.
- Ordering: §§1-2 land before §§11-12 (IPD embedded gains and NWA market effect depend on corrected basis/prices, per REQ-NWA-001's stated dependency).
