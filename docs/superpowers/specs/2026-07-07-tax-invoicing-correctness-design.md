# Tax & Invoicing Correctness — Design Spec

**Date:** 2026-07-07
**Author:** Travis Sparks (with Claude Code)
**Status:** Approved design → ready for implementation plan
**Branch:** `feat/remediation-and-features-2026-07`
**Scope:** REQ-FIX-TAX-001..007, REQ-FIX-INV-001..005, REQ-FIX-API-001..004 (Program 2026-07)
**Constraints:** cash-basis; DB sign convention per CLAUDE.md (expenses negative, income positive); `Decimal(str(x))` everywhere; never delete rows; TDD with REQ-IDs.

---

## 1. Bug inventory (verified in code)

| # | Bug | Where |
|---|-----|-------|
| P0 | Shopify payouts booked `direction=income`, `tax_category=SALES_INCOME` — every dollar counted once as order and again as payout; inflates WA B&O gross + Form 1065 L1a | `shopify_adapter._parse_payout` (~265) |
| P0 | Order income is sales-tax-inclusive everywhere except the DOR upload — `retail_facts` is used only by `generate_dor_upload`; B&O CSVs, FreeTaxUSA, TaxAct, dashboard summary all use `abs(amount)` on `total_price` | `_parse_order` ~141; `bno_tax` ~91; `freetaxusa._abs_amount`; `taxact._aggregate`; `tax_export` ~867 |
| P1 | `OTHER_EXPENSE` (Shopify refunds, contra-revenue) absent from `SCHEDULE_C_LINES` and both `FORM_1065_LINES` — filed net income overstated by refund total | `freetaxusa.py:23-39,263-281`, `taxact.py:32-49` |
| P1 | `WHOLESALE_INCOME` in `INCOME_CATEGORIES` but not `SCHEDULE_C_LINES` → filtered before summation | `freetaxusa.py:151` |
| P1 | 1099-B: present-but-null `tax_subcategory` → `"long" in None` TypeError → 500 on personal export | `freetaxusa.build_1099b_csv:107` |
| P2 | B&O grand totals accumulate unrounded Decimals while rows display `.2f` → totals ≠ sum of rows | `bno_tax.py:139-166,219-251` |
| P2 | DOR upload silently emits sentinel `____` location code (`UNKNOWN_WA_LOCATION`) | `bno_tax.py:381-384` |
| P0 | Email-fail after link creation deactivates the Stripe link but leaves `payment_link_url/id` persisted → retry reuses the **deactivated** link | `invoices.send_invoice:774-798`, `payment_link.py:22` |
| P1 | `PATCH /invoices/{id}` recomputes total but never touches the stored link; reuse never verifies amount | `invoices.py:587-616` |
| P1 | `match_payment`: no status/direction/uniqueness guards; partial-branch audit records `tx.id` as the *old* value (assignment precedes audit); full-branch hardcodes `None` | `invoices.py:1120-1175` |
| P1 | Calendar generation dedupes against DB only — duplicates **within one batch** both insert (double billing) | `generator.py:203-266`, route ~870 |
| P2 | `total_price = hours × rate` unquantized; Stripe `unit_amount = int(total * 100)` truncates sub-cent | `generator.py:252`, `payment_link.py:41` |
| P1 | `list_transactions` header totals include `rejected`/`split_parent` rows unless caller filters them | `transactions.py:471-494` |
| P1 | `get_aggregations` excludes `rejected` but not `split_parent` → splits double-count in charts/vendor totals/anomaly baselines | `transactions.py:542,579` |
| P2 | Weekly P&L window is 8–13 days depending on run day; reimbursable inflows counted as revenue | `scripts/weekly-pl-report.py:47-68` |
| P2 | Outbound contact literals mix `travis@sparkry.com` (uncontrolled domain) and `travis@sparkry.ai` | `email_sender.py:22,193,254`, `bno_tax.py:363` |

## 2. REQ-FIX-TAX-001 — Shopify payout reclassification

### 2.1 Adapter (forward fix)

`_parse_payout` mirrors `stripe_adapter._map_payout` exactly:

```python
"direction": Direction.TRANSFER.value,
"tax_category": None,
"status": TransactionStatus.AUTO_CLASSIFIED.value,
# same rationale comment as Stripe: a payout is unambiguously a transfer;
# needs_review would expose it to the reclassify pass, where the Tier-3 LLM
# mislabels "Shopify Payout" as SALES_INCOME.
```

Amount stays positive (transfer convention per CLAUDE.md). Orders remain the sole
P&L income (`direction=income`, `SALES_INCOME`, gross); refunds remain
`OTHER_EXPENSE` contra-revenue. **Reconciliation pairing is unchanged and already
correct:** `src/utils/reconciliation.py` treats Shopify as a payout source
(RECON-001..004) and pairs payout transactions with bank deposits by exact amount
± date window — identical to Stripe. Post-fix, the pair is transfer↔deposit
(neither on P&L); the orders the payout settles are the only income rows.

### 2.2 Backfill migration (existing rows)

Alembic **data migration** (validated with the `alembic-migration` skill invariants:
no deletes, no raw DELETE on protected tables, real downgrade).

- **Selection:** `source='shopify' AND source_id LIKE 'payout_%' AND direction='income'`
  (the `direction` predicate makes both directions idempotent on re-run).
- **Upgrade:** per row, set `direction='transfer'`, `tax_category=NULL`, bump
  `updated_at`. **Status-preserving:** `status`, `confirmed_by`, `raw_data` untouched —
  a human-`rejected` payout stays rejected; `needs_review` stays `needs_review`
  (only *new* rows get `auto_classified`).
- **Audit:** two transaction-mode `AuditEvent` rows per row (`direction`:
  `income`→`transfer`; `tax_category`: `SALES_INCOME`→`NULL`),
  `changed_by='migration:<revision>'`, inserted via `op.get_bind()` + SQLAlchemy core
  so `ck_audit_events_exactly_one_target` is satisfied.
- **Downgrade:** deterministic inverse (all selected rows uniformly held
  `income`/`SALES_INCOME`): restore both fields, write reversal AuditEvents; never
  delete the upgrade's events (audit is append-only in both directions).
- Migration prints the affected row count; the runbook records it as evidence.

## 3. REQ-FIX-TAX-002 — one pre-tax basis for all export surfaces

**Move, don't copy.** New module `src/export/basis.py` owns the canonical amount
computation; `retail_sales_tax.py` re-exports for backward compatibility.

```python
# src/export/basis.py
def retail_facts(tx) -> RetailFacts: ...  # moved verbatim from retail_sales_tax
def pretax_abs_amount(tx) -> Decimal:
    """SALES_INCOME rows: retail_facts(tx).pretax (raw total_price - total_tax,
    quantized); no-tax_lines rows degrade to abs(amount) — collected tax is only
    excludable when substantiated, mirroring the interstate rule.
    Everything else: abs(Decimal(str(amount))) * deductible_pct."""
```

Call sites converted (each currently open-codes `abs(amount) * pct`):
`bno_tax._aggregate_income_by_month` (~91); `freetaxusa._abs_amount` and
`taxact._abs_deductible` (delegate for income categories); `tax_export.py` summary
aggregation (~867) and the monthly B&O table (~907).

`generate_dor_upload` keeps calling `compute_retail_detail` (now importing
`retail_facts` from `basis`) — its numbers are unchanged; the other four surfaces
converge onto the same figure. Expense categories are unaffected
(`pretax_abs_amount` only diverges from `abs·pct` for retail income rows).
`deductible_pct` still applies after the pre-tax computation.

## 4. REQ-FIX-TAX-003/004 — line-mapping additions

- `freetaxusa.SCHEDULE_C_LINES += {"OTHER_EXPENSE": ("L27a", "Other expenses"),
  "WHOLESALE_INCOME": ("Gross receipts", "Wholesale income")}`. Sch C Part V flows to
  L27a. (Strictly, sales refunds are Sch C **L2 Returns and allowances** / 1065
  **L1b** — net income is identical either way; L27a/L21 chosen per REQ to avoid
  restructuring the L1a/L3 arithmetic in both exporters. Equivalence documented here
  for a future filer.)
- `freetaxusa.FORM_1065_LINES += {"OTHER_EXPENSE": ("L21", "Other deductions — other expenses")}`;
  `taxact.FORM_1065_LINES += {"OTHER_EXPENSE": ("21", "Other deductions — other expenses", False)}`.
- `taxact` inherits Sch C via the shared `SCHEDULE_C_LINES` import — one edit covers
  both (REQ-FIX-TAX-004). `tax_export.IRS_LINE_MAPPING` already maps both — no change.

## 5. REQ-FIX-TAX-005/006/007 — export hardening

- **005:** `build_1099b_csv`: `subcategory = (tx.get("tax_subcategory") or "").lower()`;
  `term = "Long" if "long" in subcategory else "Short"` (case-insensitive, None-safe).
- **006 (rounding rule):** in both B&O CSV builders, quantize **before** accumulate:
  `amt = amt.quantize(Decimal("0.01"), ROUND_HALF_UP)`; `tax = (amt * rate).quantize(...)`;
  grand totals sum the already-quantized values. Invariant: parsing the CSV and
  summing the rows reproduces the TOTAL row exactly.
- **007 (DOR hard-fail):** `generate_dor_upload` raises
  `ValueError("DOR upload blocked: N order(s) map to unmapped WA locality '____' — add the locality to WA_LOCATION_CODES: <order ids/localities>")`
  whenever any `detail.by_location` entry carries `UNKNOWN_WA_LOCATION[0]`.
  The route surfaces it as HTTP 422. Never emit a `TAX,45,____,...` line.

## 6. REQ-FIX-INV-001/002 — payment-link state machine

New invariant: **persisted link fields (`payment_link_url`, `payment_link_id`,
new column `payment_link_amount`) are either all set and the link is active at the
current total, or all NULL.** Additive Alembic migration adds
`invoice.payment_link_amount NUMERIC NULL`.

```
                      create_payment_link(inv)
   ┌──────────┐   total>0, no stored link   ┌────────────────────┐
   │ NO_LINK  │ ───────────────────────────▶│ ACTIVE(amount=T)   │
   │ (fields  │                             │ fields persisted,  │
   │  NULL)   │◀──┐                         │ amount recorded    │
   └──────────┘   │                         └────────┬───────────┘
        ▲         │ email send fails:                │
        │         │ deactivate + CLEAR fields        │ send_invoice re-send,
        │         │ + audit (INV-001)                │ total unchanged:
        │         └───────────────────────────── ────┤ REUSE (verify
        │                                            │ payment_link_amount == total;
        │  PATCH changes total (INV-002)             │ mismatch → invalidate path)
        │  or status→void (existing):                ▼
        └── deactivate old link + CLEAR fields ── [INVALIDATED at Stripe]
            + audit; next send creates fresh          (never persisted)
```

- `create_payment_link` gains an amount check: reuse only when
  `invoice.payment_link_amount == invoice.total`; creation returns the amount so the
  route persists all three fields atomically.
- `send_invoice` email-failure handler (INV-001): deactivate (best-effort, logged) and
  **always** clear the three fields + commit + AuditEvent (`payment_link_id`
  old→None) — regardless of `freshly_created`. Retry therefore mints a fresh link.
- `update_invoice` (PATCH, INV-002): whenever the recomputed total differs and
  `payment_link_id` is set → `_stripe_deactivate_link` (best-effort) + clear fields
  + AuditEvent, unconditionally on total-change.
- Stripe deactivation failures never block the state transition (link limps as a
  1-use orphan; the persisted fields are the source of truth) — matches void behavior.

## 7. REQ-FIX-INV-003 — match_payment guards

Ordered guards, each a distinct 422 (404s unchanged):

1. `inv.status in {SENT, OVERDUE}` — DRAFT ("send the invoice first"), PAID
   ("already paid — void first to re-match"), VOID rejected. (There is no PARTIAL
   enum value: a partially-paid invoice remains `sent` with
   `payment_transaction_id` set; re-matching a better transaction is allowed and
   audited with the true prior id.)
2. `tx.direction == income` and `tx.status != rejected`.
3. **Uniqueness:** no *other* invoice row has `payment_transaction_id == tx.id`
   (`Invoice.id != invoice_id`); one bank credit can pay at most one invoice.
4. **Audit truth:** capture `old_payment_id = inv.payment_transaction_id` **before**
   any assignment; both branches pass it as `old_value` (fixes the partial branch's
   self-referential audit and the full branch's hardcoded `None`).

## 8. REQ-FIX-INV-004 — calendar batch dedupe

`CalendarSession` gains optional `start_time: str | None` / `end_time: str | None`
(the iCal parser already knows them; manual UI leaves them null). The route builds
`key = (s.date, s.start_time or "", s.end_time or "", s.description.strip())` and
collapses duplicates **within the submitted batch** (first occurrence wins; response
notes drop count) before the existing DB-side double-billing guard runs. Genuinely
identical same-day sessions must be disambiguated by time or description — that is
the guard working as intended.

## 9. REQ-FIX-INV-005 — cent quantization points

`CENT = Decimal("0.01")`, `ROUND_HALF_UP`, applied at exactly three points:

1. `generator._gen_calendar` / `_gen_flat` line items:
   `total_price = (hours * rate).quantize(CENT)`; `subtotal = Σ` quantized lines
   (exact by construction). Same rule in the PATCH line-item rebuild
   (`invoices.py:593-613`).
2. `invoice.total = (subtotal + adjustments + tax).quantize(CENT)`.
3. `payment_link.create_payment_link`:
   `unit_amount = int((Decimal(str(invoice.total)) * 100).quantize(Decimal("1"), ROUND_HALF_UP))`
   — never `int()` truncation. With (1)/(2) this is a defensive identity: PDF, email,
   and Stripe amounts render the same stored, already-quantized fields, so
   `pdf_renderer`/`email_sender` need no change.

## 10. REQ-FIX-API-001/002 — totals & aggregation exclusion semantics

**Semantics:** `rejected` = excluded ledger row; `split_parent` = container whose
children carry the amounts (summing both double-counts). Item *lists* may still show
them when the caller filters for them; **money aggregates never include them.**

- `list_transactions` (~471): the totals subquery (`_ids_subq`) gains unconditional
  `Transaction.status.notin_([REJECTED, SPLIT_PARENT])` — independent of the
  caller's `status` filter. The paged `items` and `total` count keep honoring
  caller filters (you can still list rejected rows; the header just won't sum them).
- `get_aggregations` (~542): add `!= SPLIT_PARENT` to the main query **and** to the
  `all_expense_q` anomaly-baseline query (~579), alongside the existing
  `!= REJECTED`. Time-series, vendor totals, category totals, and anomaly baselines
  all inherit it.

## 11. REQ-FIX-API-003 — weekly P&L window + reimbursable netting

- **Window:** exact half-open `[last_monday, this_monday)` (7 days) regardless of run
  day: `this_monday = today - timedelta(days=today.weekday())`; `week_start =
  this_monday - 7d`; filters `date >= week_start AND date < this_monday` (ISO string
  compare is safe). Report footer prints both bounds.
- **Reimbursable netting** (per CLAUDE.md: pairs net to zero on P&L): revenue query
  additionally excludes income rows that are reimbursement receipts —
  `Transaction.id NOT IN (SELECT reimbursement_link FROM transactions WHERE
  reimbursement_link IS NOT NULL)`; the expense query already excludes
  `direction=reimbursable` (it filters `direction == "expense"`). Unlinked
  reimbursables stay invisible to both sides — correct, they are not yet P&L.
  Also exclude `split_parent` from both sums (§10 semantics).

## 12. REQ-FIX-API-004 — email domain constant

New `src/utils/constants.py`: `SPARKRY_CONTACT_EMAIL = "travis@sparkry.ai"` and
`INVOICE_FROM_ADDRESS = f"Sparkry LLC <{SPARKRY_CONTACT_EMAIL}>"`. Replaces: `email_sender.FROM_ADDRESS` (:22), the HTML footer literal (:193), the
reply-to/contact literal (:254), and the DOR preparer email in
`bno_tax.generate_dor_upload` (:363). `sparkry.com` is not a domain we control —
grep-gate in the test suite asserts no `@sparkry.com` literal remains under `src/`
(the env-default in `src/alerts/` is REQ-FIX-ALR-003's scope, excluded here).

## 13. Test strategy (TDD — failing test first, REQ-ID in test name/docstring)

**Golden-file export tests** — new `tests/fixtures/tax-export-golden/` (pattern:
`tests/fixtures/{brokerage,plaid}-golden/`): one frozen synthetic-transaction set
(orders w/ `total_tax` + WA/OOS tax_lines, refunds, payouts, wholesale rows, split
parent+children, rejected rows, a None-subcategory 1099-B row) plus expected outputs
for both B&O CSVs, `dor_upload`, Sch C, both 1065s, and 1099-B. Regenerable via a
checked-in script; diffs reviewed like code.

| REQ | Tests (co-located `test_*.py`) |
|-----|-------------------------------|
| TAX-001 | `_parse_payout` field assertions mirror the Stripe payout test; migration test: seed pre-fix rows (incl. one `rejected`, one `confirmed`) → upgrade → direction/category flipped, statuses preserved, 2 AuditEvents/row, count printed; downgrade restores + reversal events; re-run idempotent. Reconciliation test: transfer-direction payout still pairs with bank deposit. |
| TAX-002 | Unit: `pretax_abs_amount` (taxed order, no-raw_data order, expense row, deductible_pct). Golden: all four surfaces report identical `SALES_INCOME` gross == DOR retailing basis. |
| TAX-003/004 | Golden Sch C shows L27a + wholesale in gross receipts; both 1065s show L21 other deductions; assert refund reduces net by its amount. |
| TAX-005 | Rows with `tax_subcategory=None` / `"LONG-TERM"` → no raise, correct term. |
| TAX-006 | Property-style: parse generated CSV, `Σ rows == TOTAL` (seed amounts engineered to expose the old drift, e.g. many ×.005 taxes). |
| TAX-007 | Order with unmapped locality → `ValueError` naming `____` + locality; route test → 422. Mapped-only input still emits code-45 lines. |
| INV-001/002 | Mock Stripe + failing email: fields NULL after failure, audit written, retry calls `PaymentLink.create` anew. PATCH total-change clears link; no-change PATCH keeps it; reuse with mismatched `payment_link_amount` re-creates. |
| INV-003 | Guard matrix (draft/paid/void invoice; expense/rejected tx; tx already linked to another invoice) → 422 each; happy path audit `old_value` equals genuine prior id (regression for the partial-branch bug). |
| INV-004 | Batch with an exact duplicate session → one line item + drop count; distinct-time same-description pair → two items. |
| INV-005 | 1.333h × $150 → line 199.95 exact; subtotal == Σ lines; mocked Stripe receives `unit_amount` == `total*100` (regression: value that truncates under `int()`). |
| API-001/002 | Seed rejected + split parent/children; header totals and every aggregation bucket count children only, with and without caller status filters. |
| API-003 | Freeze clock on Wed + Mon: window always exactly `[Mon,Mon)`; linked reimbursable pair nets to zero in both revenue and expenses. |
| API-004 | Import-level constant assertions + `grep`-style test over `src/` for `@sparkry.com` (allowlist: `src/alerts/`). |

Gates before commit: `pytest && ruff check src/ && mypy src/`; multi-file phases via `/qpipeline thorough` per CLAUDE.md.

## 14. REQ traceability

| REQ | Design § | Primary files touched |
|-----|----------|----------------------|
| REQ-FIX-TAX-001 | §2 | `src/adapters/shopify_adapter.py`, new Alembic data migration, `src/utils/reconciliation.py` (tests only) |
| REQ-FIX-TAX-002 | §3 | new `src/export/basis.py`; `bno_tax.py`, `freetaxusa.py`, `taxact.py`, `retail_sales_tax.py`, `src/api/routes/tax_export.py` |
| REQ-FIX-TAX-003 | §4 | `freetaxusa.py`, `taxact.py` |
| REQ-FIX-TAX-004 | §4 | `freetaxusa.py` (shared constant) |
| REQ-FIX-TAX-005 | §5 | `freetaxusa.py` |
| REQ-FIX-TAX-006 | §5 | `bno_tax.py` |
| REQ-FIX-TAX-007 | §5 | `bno_tax.py`, `tax_export.py` route |
| REQ-FIX-INV-001 | §6 | `src/api/routes/invoices.py` |
| REQ-FIX-INV-002 | §6 | `invoices.py`, `src/invoicing/payment_link.py`, additive migration (`payment_link_amount`) |
| REQ-FIX-INV-003 | §7 | `invoices.py` |
| REQ-FIX-INV-004 | §8 | `invoices.py` (route + `CalendarSession`) |
| REQ-FIX-INV-005 | §9 | `src/invoicing/generator.py`, `payment_link.py`, `invoices.py` PATCH |
| REQ-FIX-API-001 | §10 | `src/api/routes/transactions.py` |
| REQ-FIX-API-002 | §10 | `transactions.py` |
| REQ-FIX-API-003 | §11 | `scripts/weekly-pl-report.py` |
| REQ-FIX-API-004 | §12 | new `src/utils/constants.py`; `email_sender.py`, `bno_tax.py` |

## 15. Rollout & invariants

1. Order: §12 constant → tax fixes (§2–5 with golden files) → invoicing (§6–9) →
   aggregation (§10–11). The TAX-001 backfill runs on the box only after the code
   deploy (`alembic upgrade head` via the standard rsync+restart flow).
2. Post-backfill verification on the box: BlackLine B&O gross for each 2026 period
   drops by exactly the reclassified payout total; before/after recorded in the runbook.
3. Standing invariants: no transaction deleted; `raw_data` untouched; every field-level
   change audited; all importer/backfill entry points DRY-RUN by default.
