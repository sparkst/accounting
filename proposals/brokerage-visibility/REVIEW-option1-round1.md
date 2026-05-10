# Review synthesis — Option 1 PLAN, round 1

3 reviewers, clean context, parallel. Findings deduplicated by topic, max severity wins.

## P0 (must fix before execute phase)

| ID | Topic | Source | Resolution |
|---|---|---|---|
| P0-001 | Plan-wrapper double-counts $149k in net worth | R3 confirmed via live DB; R1 P1 echoed | Exclude `is_plan_wrapper=True` from net-worth sum. Fixture seeds wrapper+child pair. |
| P0-002 | 3 Vanguard accounts have zero snapshots | R3 | Don't silently zero them. Show zero-snapshot accounts in a separate "Awaiting snapshot data" section; net-worth caveat reports the count. |

## P1 (must fix in plan)

| Topic | Sources | Resolution |
|---|---|---|
| `python -m scripts.brokerage-summary` contradicts dash filename | R1, R2 | Rename script to `brokerage_summary.py` (underscore). Allows both `-m` and direct invocation; resolves cleanly. |
| Test file outside pytest testpaths | R2 | Co-locate tests under `src/reports/test_brokerage_summary.py`. Pure functions move to `src/reports/brokerage_summary.py`. CLI shim at `scripts/brokerage_summary.py` imports from `src.reports.brokerage_summary`. |
| No shared `_latest_snapshots_subquery` helper | R2 | Single helper `_latest_position_snapshots(session)` returns `(account_id, symbol, max_as_of)` subquery. compute_net_worth, get_account_summary, get_top_holdings all compose against it. |
| Paired dividend/reinvest shown twice | R1, R3 | TASK-05 filters `is_synthetic=False` AND suppresses any row where `paired_transaction_id IS NOT NULL` AND `canonical_action='reinvest'` (keep the dividend side, hide the reinvest partner). |
| Status filter missing — REJECTED would appear | R3 | TASK-05 filters `status != 'rejected'`. |
| Term fallback `>= 365` mis-classifies | R3 | Use `> 365`. Better: prefer `lt_gain_loss`/`st_gain_loss` columns when non-NULL → fall back to `term` → fall back to `(closed - opened).days > 365` → if `opened_date IS NULL` bucket as "unknown". |
| Wash-sale handling unspecified | R1, R2, R3 | gain_loss is broker-pre-adjusted; use it as-is for ST/LT totals. Surface wash-sale lots and disallowed_loss as a separate footer line. Fixture seeds a wash-sale row. |
| Zero-quantity sold-off positions in top holdings | R2 | TASK-04 filters `market_value > 0 OR quantity > 0`. Fixture seeds a zero-qty case. |
| No read-only invariant test | R2 | Add fixture-level assert after every pure-function call: `not session.dirty and not session.new and not session.deleted`. |
| TDD RED phase not enforced | R2 | Add explicit two-step protocol per task in plan: write test → confirm `pytest -k <name>` errors with NameError/AttributeError → implement → green. |
| Idempotency / row counts not visible in report | R1 | Add a "Data integrity" footer to the report: total accounts, transactions, snapshots, realized lots — match against the known baseline. |
| Synthetic rows inflate transaction counts | R1, R3 | Same fix as paired dividend/reinvest above. TASK-05 filters `is_synthetic=False`. |
| NULL-symbol cash misclassification ($389k equity at risk) | R3 | Cash sleeve identified by *known ticker* (CASH/SPAXX/FDRXX/VMFXX/SWVXX/SWLXX/MMDA1) only. NULL-symbol positions stay in top holdings displayed by description. Add an "Unmapped tickers" sub-section if any NULL symbols appear with material market value. |

## P2 (worth fixing now while we're here)

- **Tax-sheltered column in Accounts section** [R1] → trivial add, keeps Option 1 honest about REQ-005a.
- **Stale snapshot date range** [R3] → show `(min as_of) … (max as_of)` next to net-worth total.
- **Per-entity subtotal** [R1 says defer] → **DEFER to Option 2** per consensus. Net Worth Summary shows total + per-broker only.
- **Decimal precision** [R2] → quantize to `0.01` before currency formatting; assert `Decimal('12345.678')` renders `$12,345.68`.
- **Performance assert** [R2] → integration test asserts elapsed < 2.0s for fixture run.
- **Empty-DB asserts on TASK-04/05/06** [R2] → add to each.
- **Z-prefix masking** [R2] → fixture includes `Z23257759`; assert masked → `****7759`.
- **pct_of_net_worth denominator** [R3] → `get_top_holdings` takes `net_worth: Decimal` parameter from `compute_net_worth`.
- **`account_number` vs `account_number_masked` key naming** [R2] → standardize to `account_number_masked` everywhere; helper `_mask_account_number(s) -> str`.
- **Orphan transaction integrity** [R1] → Data integrity footer reports orphan count.
- **TASK-08 `--db /nonexistent`** [R2] → use `/tmp/no-such-dir/x.db` so OS-level error is guaranteed.
- **TASK-01 entity diversity** [R2] → seed accounts across personal/sparkry/blackline so subtotal logic is testable (even though Option 1 doesn't render entity yet, the helper is shared with Option 2).

## P3 (defer or address only if cheap)

Cash-sleeve symbol list expansion (covered above), TASK-05 timezone (use `date.today()` per stdlib default), TASK-05 fixture out-of-window row (folded into TASK-01 update), `lt_gain_loss/st_gain_loss` priority (covered above).

## Strategic items NOT escalated to user

The user said: "Use /qreview to get consensus on decisions before escalating to me. Only include me in the really big strategic decisions." None of these are strategic — they're all "match Phase 1 data correctly". Decisions taken by claude per consensus:

1. Plan-wrapper exclusion: `is_plan_wrapper=True` rows excluded from net worth (the BrokerageLink child holds the actual positions).
2. Zero-snapshot Vanguard accounts: flagged in report rather than blocking on re-ingest. The whole point of Option 1 is to surface gaps — we surface this one.
3. Per-entity subtotal deferred to Option 2.
4. Cash sleeve identification: ticker-based, never NULL-symbol-based.
5. Wash-sale: gain_loss used as-is; disallowed_loss surfaced as informational footer only.

## Action

Update PLAN-option1.md to v2 with all P0/P1/P2 fixes folded in. Re-review (round 2) to confirm convergence. Then proceed to execute phase.
