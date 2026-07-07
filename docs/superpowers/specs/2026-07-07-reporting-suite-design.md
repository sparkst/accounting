# Reporting Suite (WBR · Tax Forecaster · Sellability) — Design Spec

**Date:** 2026-07-07 · **Author:** Travis Sparks (with Claude Code) · **Status:** Draft
**Scope:** REQ-WBR-001..003, REQ-TXF-001..004, REQ-SEL-001..002 (Program 2026-07)
**Branch:** `feat/remediation-and-features-2026-07`

---

## 1. Goal & delivery decision

Three deterministic, Decimal-only, no-LLM email reports with Amazon-WBR discipline:
diff-first, every number flagged against a threshold, zero filler.

**Delivery split (locked 2026-07-07):** Telegram keeps pulse + sev alerts (n8n severity
webhook, unchanged). These three reports go by **email via Resend directly** (the
`src/invoicing/email_sender.py` pattern) — documents to read, not interrupts. Body is
monospace plain text wrapped in `<pre>` for the HTML part; the plain text IS the report
and is what golden tests assert on. No design chrome.

## 2. Shared architecture

```
src/reports/pl_engine.py    — entity P&L compute (extracted by REQ-FIX-API-003 from
                              scripts/weekly-pl-report.py; WBR/SEL/TXF import it, never re-derive)
src/reports/report_email.py — Resend sender + alert_dispatch ledger recording (shared)
src/reports/report_config.py— YAML loader (safe_load, Decimal coercion at boundary)
src/reports/wbr.py          — WBR compute + render        scripts/wbr_dispatch.py
src/reports/tax_forecast.py — TXF compute + render        scripts/tax_forecast_dispatch.py
src/reports/sellability.py  — SEL compute + render        scripts/sellability_dispatch.py
deploy/accounting-{wbr,tax-forecast,sellability}.{service,timer}
config/reporting.yaml · config/tax_profile.yaml (gitignored; .example checked in)
config/tax_tables/2026.yaml · config/sellability.yaml
```

Conventions (all three, per `src/reports/brokerage_summary.py` house style):
- Session-first public compute fns returning `TypedDict`s; pure `render_report(data) -> str`;
  `_today()` indirection for test pinning; explicit `__all__`.
- **Decimal math only**: `Decimal(str(x))` at every DB/YAML boundary; `quantize(Decimal("0.01"))`
  before render. Two runs over the same DB produce byte-identical output.
- **DRY-RUN default**: CLI is argparse with `--apply` (send email + write ledger) and
  `--date YYYY-MM-DD`; dry-run prints the rendered report to stdout and writes nothing.
- **Send ledger**: every apply-mode send recorded in the existing `alert_dispatch` table
  (`alert_type` ∈ `wbr_weekly | tax_forecast | sellability_monthly`, `alert_key` =
  `wbr:2026-W28` / `txf:2026-Q3` / `sel:2026-06`; `UniqueConstraint(alert_key,
  occurrence_date)` gives idempotent re-runs — a `Persistent=true` catch-up cannot double-send).
- **Email guards**: single `FROM_ADDRESS` constant per REQ-FIX-API-004 (`travis@sparkry.ai`);
  recipient from `REPORT_TO_EMAIL` env (default `Travis@sparkry.com`), format-validated and
  allowlisted, mirroring the REQ-FIX-ALR-003 pattern. `RESEND_API_KEY` via Doppler
  (`accounting/srv` on the box, `accounting/dev` locally).
- `config/` is a new top-level directory; `report_config.py` is the single loader. Missing
  optional config → coded defaults + a visible "(defaults)" marker in the report.

## 3. WBR scorecard (REQ-WBR-001..003)

### 3.1 Email layout

Subject: `[WBR] <ISO-week> · HH net <±$X> (<±%> WoW) · <N>⚠️` — e.g.
`[WBR] 2026-W28 · HH net +$4,120 (▲12%) · 2⚠️`. Zero warnings renders `0⚠️`. Body:

```
WBR — week 2026-07-06 → 2026-07-13 (Mon–Mon, half-open)          run 2026-07-13 06:00 PT

P&L (WoW)                     this wk      last wk      Δ$        Δ%     6wk trend
Sparkry    revenue            $8,250       $8,250       $0        0.0%   ▃▃█▃▃▃  ✅
           expenses           ($1,412)     ($987)       ($425)   +43.1%  ▂▂▃▂▂▅  ⚠️ >+30%
           net                $6,838       $7,263       ($425)   -5.9%   ▃▃█▃▃▃  ✅
BlackLine  revenue / exp / net  …one row each, same columns…
Personal   …
HOUSEHOLD  net                $7,102       $6,341       +$761    +12.0%  ▄▃█▄▃▅  ✅

AR AGING            current   15–30d    31–45d    45d+      total
                    $33,000   $0        $0        $0        $33,000                    ✅

CASH (Plaid, latest snapshot per account)
Sparkry Checking …1234        $18,412   (as of 2026-07-12)                              ✅
BlackLine Checking …5678      $2,104    (as of 2026-07-12)                              ⚠️ <$2,500
Amex …0005 (liability)        ($3,310)  (as of 2026-07-12)                              ✅

OPS                 review queue: 14 ✅   auto-confirmed this wk: 22 ✅
DELIVERY HEALTH     plaid items: 2/2 ok (max age 1d) ✅ · alerts 7d: 9 sent / 0 failed ✅
                    unmapped plaid accounts: 0 ✅ · snapshot gap days: 0 ✅

⚠️ SUMMARY (act on these)
1. Sparkry expenses +43.1% WoW (threshold +30%): AWS annual renewal $389 (txn 8f2c…)
2. BlackLine Checking $2,104 < $2,500 floor

Data as-of — register: gmail 07-12 · stripe 07-13 · plaid-txn 07-13 · bank-csv 07-01 ⚠️stale
plaid balances: 07-12 · invoices: live · alert ledger: live        thresholds: config v1
```

The ⚠️ SUMMARY names the *cause* (largest contributing txn/vendor for P&L breaches) — the
diff, not just the flag. Sparkline: 6 weekly net values min-max scaled to `▁▂▃▄▅▆▇█`.

### 3.2 Data sources & tie-out rules (REQ-WBR-002)

| Metric | Source | Tie-out rule |
|---|---|---|
| Per-entity rev/exp/net | `pl_engine.compute_entity_pl(session, start, end, entity)` | Exact half-open `[Mon, Mon)` 7-day window on `Transaction.date` (string compare, ISO-safe); `status NOT IN (rejected, split_parent)`; `sum(abs(amount))` by direction; income positive / expenses rendered `($x)` per app convention; **reimbursable pairs netted**: `direction=reimbursable` rows and their `reimbursement_link` income counterparts excluded from both sides (REQ-FIX-API-003 engine — WBR must equal `weekly-pl-latest.txt` for the same window, asserted by test) |
| 6wk trend | Same engine, 6 consecutive `[Mon, Mon)` windows | Identical predicates per window |
| AR aging | `Invoice` where `status IN (sent, overdue)` | Age = days since `sent_at` (fallback `submitted_date`); buckets 0–14 / 15–30 / 31–45 / 45+ — same rungs as REQ-ARC-001, so the AR chaser and WBR always agree |
| Cash positions | Latest `PlaidAccountBalanceSnapshot` per `account_id` (max `snapshot_date`) | `PLAID_LIABILITY_TYPES` (`credit`,`loan`) negated at read, matching net-worth convention; per-account `(as of <date>)` always shown |
| Review queue | `count(*) WHERE status='needs_review'` | Same predicate as `/transactions/review` default |
| Auto-confirmed | `AuditEvent` count, `changed_by LIKE 'auto%'`, week window | Feeds the REQ-MCA-003 digest number |
| Delivery health | `ingestion_log` (max `run_at` + `status` per source) + `alert_dispatch` (7d sent/failed) + unmapped-account log rows (REQ-FIX-PLD-005) + snapshot-gap days | Weekly rollup of the same queries as the daily pulse block (REQ-DHL-001) — shared helper, one definition |

### 3.3 Default thresholds (`config/reporting.yaml`, coded fallbacks)

| Metric | ⚠️ when | Default |
|---|---|---|
| Entity net WoW | drop > `net_drop_pct` AND > `min_abs` | 30% / $500 |
| Entity expenses WoW | rise > `exp_rise_pct` AND > `min_abs` | 30% / $500 |
| Sparkry weekly revenue | `= $0` | on |
| AR 31–45d / 45+ buckets | > $0 | on |
| AR total | > `ar_total_max` | $30,000 (matches weekly-P&L flag) |
| Checking account balance | < `checking_floor` | $2,500 per account |
| Any liability (credit) balance | > `credit_max` | $8,000 |
| Review queue depth | > `review_max` | 25 |
| Plaid item last-success age | > `plaid_stale_days` | 2 days |
| Alert failures (7d, unresolved) | > 0 | on |
| Unmapped Plaid accounts | > 0 | on |
| Source freshness | older than per-source cadence (`freshness:` map) | plaid 2d · gmail 3d · stripe 3d · bank-csv 35d |

### 3.4 Scheduling & TZ (REQ-WBR-001/003)

`accounting-wbr.timer`: `OnCalendar=Mon *-*-* 06:00:00 America/Los_Angeles`,
`Persistent=true`. systemd resolves the TZ suffix per elapse, so DST is automatic —
13:00 UTC during PDT, 14:00 UTC during PST — no hand-rolled UTC math. Service unit clones
the `accounting-ea-alerts.service` sandbox/Doppler pattern verbatim, `ExecStart=… -m
scripts.wbr_dispatch --apply`, `OnFailure=accounting-alert@%p.service`. The report itself
computes the window from the **America/Los_Angeles** calendar date (via `zoneinfo`), so a
UTC-clock box never selects the wrong Monday. CLI on demand: `python -m scripts.wbr_dispatch
[--date …]` renders to stdout (dry-run default).

## 4. Tax-posture forecaster (REQ-TXF-001..004)

### 4.1 Projection method + assumption block

Primary: **linear YTD annualization** — `projected = ytd × Decimal(days_in_year) /
Decimal(days_elapsed)` per entity per line (gross receipts by tax category, expenses by
category), computed from the same `_fetch_transactions` predicates as `/tax-summary`
(rejected + split_parent excluded, `abs(amt) × deductible_pct`, REIMBURSABLE excluded from
both sides). **Seasonality guard**: if any single month holds > 40% of YTD gross receipts,
or YTD spans < 60 days, the projection is stamped `HIGH VARIANCE` and a second estimate —
trailing-3-month run-rate × remaining months — is printed beside it. The email always opens
with an ASSUMPTIONS block: method, days elapsed, guard status, config file mtimes, and
"projection ≠ advice; deterministic annualization of cash-basis actuals".

### 4.2 Computation pipeline (all constants from `config/tax_tables/2026.yaml`, not code)

1. **Sparkry Schedule C**: projected gross receipts − projected deductible expenses via
   `IRS_LINE_MAPPING` semantics (+ home-office constant) → projected net profit.
2. **BlackLine 1065/K-1**: projected entity net × `k1_share` (config; 1.0 — Travis 100%
   vested, Emerson 0% profits interest) → passthrough, SE-subject (active member).
3. **SE tax**: `(SchC + K-1) × 0.9235`; 12.4% SS up to `ss_wage_base` (minus W-2 SS wages
   already taxed), 2.9% Medicare unlimited + 0.9% addl over MFJ threshold; ½-SE deduction.
4. **QBI**: 20% × qualified business income, capped at 20% × (taxable income − net capital
   gains). **Phase-out check**: consulting is SSTB — if projected MFJ taxable income >
   `qbi_mfj_threshold` the report shows the phase-out band position and reduced/zero QBI;
   never silently assumes full 20%.
5. **WA B&O accrual**: projected gross receipts by category through `BO_CLASSIFICATION`
   (`src/export/bno_tax.py` — CONSULTING/SUBSCRIPTION→Service&Other 1.5%, SALES→Retailing,
   WHOLESALE→Wholesaling 0.484%); Sparkry accrues monthly, BlackLine quarterly. Rates read
   from the existing export module — one source of truth.
6. **MFJ federal position**: taxable income = business net − ½SE − QBI + W-2 + expected
   investment income − `standard_deduction_mfj`; walk `mfj_brackets` (list of
   `{up_to, rate}`) → total tax, marginal + effective rate, distance to next bracket edge.
   2026 table values shipped in the YAML are **verified against IRS Rev. Proc. at
   implementation time** (release-blocking check, not a code review nicety).

### 4.3 Safe harbor (REQ-TXF-002)

Target = `110% × prior_year_total_tax` (config). Compare vs YTD W-2 withholding +
`estimated_payments` list (both from `tax_profile.yaml`). Output one line per remaining
quarter: `Set aside $X by Sep 15 2026` — X = (target × cumulative-quarter fraction −
paid-to-date), floored at $0, quantized to dollars. Due dates Apr 15 / Jun 15 / Sep 15 /
Jan 15 in the tax-tables YAML.

### 4.4 `config/tax_profile.yaml` schema (gitignored; `.example` checked in)

```yaml
tax_year: 2026
filing_status: mfj
w2: [{employer: "...", ytd_wages: 0.00, ytd_federal_withholding: 0.00, ytd_ss_wages: 0.00}]
expected_investment_income: {interest: 0.00, dividends_qualified: 0.00, capital_gains_lt: 0.00}
prior_year_total_tax: 0.00
estimated_payments: [{date: 2026-04-15, amount: 0.00}]
```

**Business-only fallback (REQ-TXF-003)**: file missing, unparseable, or
`prior_year_total_tax` ≤ 0 → report runs with a banner saying so: business projections +
B&O + SE render fully; bracket position and safe harbor print `UNAVAILABLE — fill
config/tax_profile.yaml`. Never guesses household numbers.

### 4.5 Cadence & delivery (REQ-TXF-001/004)

`accounting-tax-forecast.timer`: `OnCalendar=*-01,04,06,09-01 07:00:00 America/Los_Angeles`,
`Persistent=true` — two weeks ahead of each estimated-tax due date; on-demand via
`python -m scripts.tax_forecast_dispatch`. Subject: `[TAX] 2026-Q3 forecast · set aside $X
by Sep 15 · SH 110%: $Y of $Z paid`. DRY-RUN default; ledger key `txf:<year>-Q<n>`.

## 5. Sellability metrics (REQ-SEL-001..002)

### 5.1 SDE and `config/sellability.yaml`

`SDE(month|TTM) = Sparkry net income (pl_engine, monthly windows, reimbursables netted)
+ add-backs`. Add-backs come only from config and are **itemized in the report** (label,
rule, amount) — audit-friendly, nothing implicit:

```yaml
addback_categories: [HEALTH_INSURANCE, PERSONAL_NON_DEDUCTIBLE]   # sum of abs(amount), Sparkry rows
owner_salary_monthly: 0.00               # SMLLC draws aren't expenses; explicit if that changes
one_time_items: [{date: 2026-03-14, description: "…", amount: 1200.00, note: "laptop"}]
recurring_customers: {Cardinal Health: true, Fascinate: true}      # override; default from billing_model
stripe_client_map: [{match: "customer:cus_ABC", client: "Substack"},
                    {match: "desc_contains:substack", client: "Substack"}]
```

### 5.2 Client attribution & recurring flag (REQ-SEL-002)

Revenue by client, Sparkry, per month + TTM:
1. **Invoices** (primary): paid invoices via `payment_transaction_id` → transaction month;
   client = `Customer.name` via `customer_id`.
2. **Stripe income** (non-invoice): map via `stripe_client_map` rules against
   `raw_data["customer"]` / `raw_data["metadata"]` / `description` (first match wins).
3. Remainder lands in an explicit `UNATTRIBUTED` row with its % — never silently dropped;
   > 10% unattributed flags ⚠️.

**Recurring flag**: derived default — `BillingModel.FLAT_RATE` or `HOURLY` with
`calendar_patterns` ⇒ recurring; `PROJECT` ⇒ project — overridable per customer (and per
Stripe-mapped client) in `recurring_customers`. Output: recurring vs project revenue split.

### 5.3 Report content

Subject `[SELL] 2026-06 close · SDE $X TTM · top-1 N%`. Sections: SDE table (net → each
add-back line → SDE, month + TTM); client revenue table with top-1/top-3 concentration %
(⚠️ defaults: top-1 > 50%, top-3 > 80%); recurring/project split; 6-month MoM trend table
(revenue, SDE, concentration); **BlackLine burn line**: monthly net + TTM cumulative,
labeled investment-mode (no sellability framing); the $50 Fidelity TOD closure prompt
(REQ-FIX-DAT-002) rides along here.

### 5.4 Delivery

Monthly, 1st, prior-month scope: `accounting-sellability.timer` `OnCalendar=*-*-01
06:30:00 America/Los_Angeles`, `Persistent=true`; ledger key `sel:<YYYY-MM>`; DRY-RUN
default. Built as composable `render_sellability_section(data)` so the monthly-close agent
(REQ-MCA-001, separate spec) embeds it in the close email when it ships; standalone until then.

## 6. Testing strategy (TDD, REQ-tagged)

- **Golden-output fixtures**: seeded in-memory SQLite (known txns/invoices/snapshots across
  all three entities, incl. reimbursable pairs, split parents, rejected rows) + pinned
  `_today()` → `render_report` compared byte-for-byte against
  `tests/fixtures/reports-golden/{wbr,txf,sel}-*.txt`. Update golden files only via
  deliberate regeneration commit.
- **Tie-out tests**: WBR entity P&L equals `pl_engine` output equals corrected weekly-P&L
  numbers for the same window (REQ-WBR-002); TXF category math equals `/tax-summary` totals
  for the fixture year; B&O rows equal `bno_tax` export for same data.
- **Determinism**: run compute+render twice on one session → identical strings; no float
  anywhere (assert via Decimal-type checks on TypedDict contents).
- Edge tests: empty week, missing plaid snapshots (footer stale markers), tax_profile absent
  (business-only banner), seasonality guard trip, unattributed-revenue bucket, DST boundary
  (window derivation on PST↔PDT transition Mondays), ledger dedup on re-run.
- Email sender mocked; webhook/Resend never hit in tests; CLI dry-run smoke test asserts
  exit 0 + no ledger writes.

## 7. Failure & ops

`OnFailure=accounting-alert@%p.service` on all three services (journal-tail email per
REQ-FIX-ALR-006). Send failures: Resend exception → ledger row `status='failed'` + non-zero
exit (never silent); the REQ-FIX-ALR-002 replay loop excludes report types — re-running the
timer/CLI regenerates fresher data instead. Rollout: manual `--apply` smoke on box → enable timers.

## 8. REQ traceability

| REQ | Where satisfied |
|---|---|
| REQ-WBR-001 | §3.1 layout, §3.3 thresholds, §3.4 Mon 06:00 PT timer |
| REQ-WBR-002 | §3.2 tie-out table, freshness footer §3.1, tie-out tests §6 |
| REQ-WBR-003 | §3.4 timer + DRY-RUN CLI, §7 OnFailure |
| REQ-TXF-001 | §4.1–4.2 projections (Sch C, K-1, B&O, SE, QBI, MFJ brackets), §4.5 cadence |
| REQ-TXF-002 | §4.3 110% safe harbor + set-aside lines |
| REQ-TXF-003 | §4.4 tax_profile.yaml + business-only fallback; Decimal/no-LLM §2 |
| REQ-TXF-004 | §2 Resend channel, §4.5 DRY-RUN |
| REQ-SEL-001 | §5.1 SDE, §5.3 concentration/recurring/MoM/BlackLine burn, §5.4 monthly close |
| REQ-SEL-002 | §5.2 attribution + recurring flag, §5.1 config add-backs itemized in report |

## 9. Out of scope (v1)

WA B&O small-business credit; state income tax; AMT/NIIT detail (NIIT is an open item);
HTML-designed emails; dashboard rendering; TXF backtesting; multi-recipient routing;
REQ-ARC reminder ladder (separate spec — WBR only displays aging).

## 10. Open items

1. REQ-FIX-API-003 must land `pl_engine.compute_entity_pl` extraction first; if it ships
   script-internal, WBR's first task is the extraction (same tests).
2. Verify 2026 IRS constants (brackets, standard deduction, SS wage base, QBI thresholds)
   against Rev. Proc. before enabling the timer — values in YAML are placeholders until then.
3. NIIT (3.8%) on expected investment income — likely add to §4.2 step 6; confirm with EA.
4. Personal-entity WBR rows vs household-net-only — ship with rows, cut if noise.
