# V2 Improvements — Technical Design

> Architecture, data flow, API changes, database changes, and UX wireframes for the V2 improvement requirements.

---

## 1. Architecture Overview

### Current Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           DATA SOURCES                                  │
│                                                                         │
│  Gmail/n8n    Stripe     Shopify    Bank CSV   Brokerage   Photo Receipt│
│  (JSON)       (API)      (API)      (Upload)   (Upload)    (Claude)     │
│     │           │          │           │          │            │         │
└─────┼───────────┼──────────┼───────────┼──────────┼────────────┼────────┘
      │           │          │           │          │            │
      ▼           ▼          ▼           ▼          ▼            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        ADAPTER LAYER (Python)                           │
│                                                                         │
│  gmail_n8n.py  stripe.py  shopify.py  bank_csv.py  brokerage.py  ocr   │
│                                                                         │
│  - Per-record error isolation                                           │
│  - SHA256 dedup (source_hash)                                           │
│  - IngestionLog per run                                                 │
│  - Normalizes to Transaction schema                                     │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    CLASSIFICATION ENGINE                                 │
│                                                                         │
│  Tier 1: VendorRule exact match (instant, confidence 0.95)              │
│  Tier 2: Pattern/regex match (fast, confidence 0.80)                    │
│  Tier 3: Claude API classification (slow, confidence varies)            │
│                                                                         │
│  Result: entity + tax_category + direction + confidence                 │
│  If confidence < 0.7 → status = needs_review                           │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     SQLite REGISTER (data/accounting.db)                 │
│                                                                         │
│  transactions    vendor_rules    ingestion_logs    audit_events         │
│  invoices        customers       invoice_line_items                      │
│  [NEW] llm_usage_logs           [NEW] tax_year_locks                    │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────────┐
              │ Dashboard│ │ Tax      │ │ Reconcile    │
              │ (Svelte) │ │ Exports  │ │ Engine       │
              │          │ │ (CSV/TXT)│ │              │
              │ Review   │ │          │ │ Stripe payout│
              │ Register │ │ FreeUSA  │ │ ↕ Bank dep.  │
              │ Tax      │ │ TaxAct   │ │              │
              │ Health   │ │ B&O      │ │ Manual match │
              │ Invoices │ │          │ │              │
              │ Accounts │ │          │ │              │
              └──────────┘ └──────────┘ └──────────────┘
```

### Invoice Lifecycle (with V2 payment automation)

```
  ┌────────┐    Generate     ┌────────┐    Mark Sent    ┌────────┐
  │        │ ─────────────→  │        │ ──────────────→ │        │
  │  (new) │                 │ DRAFT  │                 │  SENT  │
  │        │                 │        │                 │        │
  └────────┘                 └───┬────┘                 └───┬────┘
                                 │                          │
                                 │ Void                     │ past due_date
                                 ▼                          ▼
                            ┌────────┐              ┌──────────┐
                            │  VOID  │ ◄─────────── │ OVERDUE  │
                            │(term.) │    Void       │          │
                            └────────┘               └────┬─────┘
                                 ▲                        │
                                 │ Void                   │ Mark Paid
                                 │                        ▼
                            ┌────┴───┐              ┌──────────┐
                            │        │ ◄─────────── │   PAID   │
                            │  VOID  │   Void       │          │
                            └────────┘               └────┬─────┘
                                                          │
                                                          │ [NEW] Auto-create
                                                          ▼
                                                    ┌──────────┐
                                                    │ Income   │
                                                    │ Txn in   │
                                                    │ Register │
                                                    └──────────┘
```

### Review Queue Triage Flow

```
  ┌─────────────────────────────────────────────────────────────┐
  │                    REVIEW QUEUE                               │
  │                                                               │
  │  Priority 0: Amount Errors (review_reason = "Amount could")  │
  │  ──────────────────────────────────────────────────────────── │
  │  [!] Unknown amount - Amazon receipt     [Extract Receipt]    │
  │                                                               │
  │  Priority 1: Duplicate Suspects                               │
  │  ──────────────────────────────────────────────────────────── │
  │  [≈] Stripe charge matches bank deposit  [Link] [Dismiss]    │
  │                                                               │
  │  Priority 2: Low Confidence (<0.5)                            │
  │  ──────────────────────────────────────────────────────────── │
  │  [?] Anthropic - $42.50                  [Entity ▾] [Cat ▾]  │
  │                                                               │
  │  Priority 3: First-Time Vendors                               │
  │  ──────────────────────────────────────────────────────────── │
  │  [★] New vendor: EcoEnclose             [Entity ▾] [Cat ▾]   │
  │                                                               │
  │  ─── Compact Mode ─────────────────────────────────────────── │
  │  Mar 15  Anthropic     $42.50   [Sparkry▾] [Office▾]  [✓]    │
  │  Mar 14  EcoEnclose    $89.00   [BLine▾]   [COGS▾]    [✓]    │
  │  Mar 14  Amazon        $NaN     [      ▾]  [    ▾]    [OCR]  │
  └─────────────────────────────────────────────────────────────┘
```

---

## 2. Database Changes

### New Table: llm_usage_logs

```sql
CREATE TABLE llm_usage_logs (
    id          TEXT PRIMARY KEY,          -- UUID
    timestamp   DATETIME NOT NULL,
    model       TEXT NOT NULL,             -- e.g. "claude-3-haiku"
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd    REAL NOT NULL,             -- estimated cost
    transaction_id TEXT,                   -- FK to transactions.id (nullable)
    operation   TEXT NOT NULL,             -- "classification", "receipt_ocr", etc.
    created_at  DATETIME NOT NULL
);
```

### New Table: tax_year_locks

```sql
CREATE TABLE tax_year_locks (
    id          TEXT PRIMARY KEY,          -- UUID
    entity      TEXT NOT NULL,             -- sparkry | blackline | personal
    year        INTEGER NOT NULL,
    locked_at   DATETIME NOT NULL,
    locked_by   TEXT NOT NULL DEFAULT 'human',
    notes       TEXT,
    UNIQUE(entity, year)
);
```

### Schema Changes to Existing Tables

**transactions table** — no schema changes needed. The 1099 source can be stored in `raw_data` JSON field initially, avoiding a migration. If it becomes a first-class field later, add:

```sql
ALTER TABLE transactions ADD COLUMN form_1099_source TEXT;  -- payer name for 1099 reporting
ALTER TABLE transactions ADD COLUMN form_1099_ein TEXT;     -- payer EIN (sensitive)
```

**Source enum addition** (Python only):

```python
# Add to Source enum in src/models/enums.py
INVOICE = "invoice"  # Auto-created income transaction when invoice marked paid
```

**TaxCategory enum additions** (Python only, no migration needed since SQLite stores strings):

```python
# Schedule C additions
OTHER_EXPENSE = "OTHER_EXPENSE"           # Line 27
HOME_OFFICE = "HOME_OFFICE"               # Line 30 / Form 8829
UTILITIES = "UTILITIES"                    # Line 25
RENT = "RENT"                             # Line 20b
DEPRECIATION = "DEPRECIATION"             # Line 13
REPAIRS = "REPAIRS"                       # Line 21
INTEREST_BUSINESS = "INTEREST_BUSINESS"   # Line 16
```

---

## 3. API Changes

### Modified Endpoints

#### GET /api/tax-summary — Add per-month breakdown

Current response:
```json
{
  "entity": "sparkry",
  "year": 2025,
  "line_items": [...],
  "gross_income": 120000.0,
  "total_expenses": 45000.0,
  "net_profit": 75000.0,
  "readiness": {...},
  "warnings": [...]
}
```

New fields added to response:
```json
{
  "...existing fields...",
  "monthly_income": [10000, 10000, 10000, 10000, 10000, 10000, 10000, 10000, 10000, 10000, 10000, 10000],
  "quarterly_income": [30000, 30000, 30000, 30000],
  "bno_breakdown": [
    {"period": "Jan 2025", "gross_income": 10000.0, "classification": "service_and_other", "taxable": 10000.0},
    {"period": "Feb 2025", "gross_income": 10000.0, "classification": "service_and_other", "taxable": 10000.0},
    "..."
  ],
  "form_1099_summary": [
    {"payer": "Cardinal Health, Inc.", "ein": "39-4105886", "form": "1099-NEC", "amount": 99000.0},
    {"payer": "Stripe", "form": "1099-K", "amount": 21000.0}
  ],
  "estimated_tax": {
    "annual_estimated": 18750.0,
    "quarterly_amount": 4687.5,
    "payments_made": [
      {"quarter": "Q1", "due_date": "2025-04-15", "amount_paid": 4688.0, "status": "paid"},
      {"quarter": "Q2", "due_date": "2025-06-15", "amount_paid": 0, "status": "upcoming"}
    ]
  }
}
```

Implementation in `src/api/routes/tax_export.py`:
```python
def _monthly_income(transactions, year):
    """Aggregate income by month for B&O.

    B&O is based on gross receipts, so we filter by income TaxCategories
    (CONSULTING_INCOME, SUBSCRIPTION_INCOME, SALES_INCOME) rather than
    the direction field, which may not always be set correctly.

    NOTE: Decimal("0") is falsy in Python, so use `is not None` checks,
    never `tx.amount or 0`.
    """
    monthly = [Decimal("0")] * 12
    for tx in transactions:
        if tx.tax_category in INCOME_CATEGORIES and tx.date:
            month = int(tx.date[5:7]) - 1  # 0-indexed
            if 0 <= month < 12:
                amt = Decimal(str(tx.amount)) if tx.amount is not None else Decimal("0")
                monthly[month] += amt
    return [float(m) for m in monthly]
```

#### PATCH /api/invoices/{id} — Accept sap_checklist_state

Add `sap_checklist_state` to the patchable fields:
```json
{
  "sap_checklist_state": {"0": true, "1": true, "2": false, "3": false, "4": false, "5": false, "6": false, "7": false}
}
```

#### POST /api/transactions/bulk-confirm — New endpoint

Request (max 100 items per call):
```json
{
  "transaction_ids": ["uuid-1", "uuid-2", "uuid-3"],
  "entity": "sparkry",
  "tax_category": "OFFICE_EXPENSE",
  "direction": "expense"
}
```

Response:
```json
{
  "confirmed": 3,
  "failed": 0,
  "results": [
    {"id": "uuid-1", "status": "confirmed"},
    {"id": "uuid-2", "status": "confirmed"},
    {"id": "uuid-3", "status": "confirmed"}
  ]
}
```

Behavior notes:
- Each confirmed transaction triggers the learning loop (VendorRule upsert)
- Already-confirmed transactions are skipped (not errors)
- Tax year locks are checked per transaction
- Returns 422 if `transaction_ids` exceeds 100
- Partial success: some items may fail validation; response includes per-item status

#### GET /api/health — Add source config status

Add to each source freshness entry:
```json
{
  "source": "stripe",
  "freshness_status": "never",
  "config_status": "not_configured",
  "config_hint": "Add STRIPE_API_KEY_SPARKRY and STRIPE_API_KEY_BLACKLINE to .env",
  "record_count": 0,
  "last_sync": null
}
```

Config status enum: `not_configured | configured | ready | active | error`

#### GET /api/llm-usage — New endpoint

Response:
```json
{
  "month": "2026-03",
  "total_calls": 47,
  "total_input_tokens": 125000,
  "total_output_tokens": 18000,
  "estimated_cost_usd": 0.054,
  "by_operation": {
    "classification": {"calls": 42, "cost": 0.045},
    "receipt_ocr": {"calls": 5, "cost": 0.009}
  },
  "trend": [
    {"month": "2026-01", "cost": 0.032},
    {"month": "2026-02", "cost": 0.041},
    {"month": "2026-03", "cost": 0.054}
  ]
}
```

#### POST /api/tax-year-locks — New endpoint

Request:
```json
{"entity": "sparkry", "year": 2025}
```

Response:
```json
{"id": "uuid", "entity": "sparkry", "year": 2025, "locked_at": "2026-04-16T10:00:00"}
```

#### DELETE /api/tax-year-locks/{entity}/{year} — Unlock

Returns 200 on success with confirmation.

---

## 4. UX Wireframes

### Compact Review Queue Card

```
┌──────────────────────────────────────────────────────────────────────┐
│ ☐  Mar 15  Anthropic, PBC           -$42.50  [Sparkry ▾] [Office ▾] ✓│
│ ☐  Mar 14  EcoEnclose, Inc.         -$89.00  [BLine  ▾] [COGS   ▾] ✓│
│ ☐  Mar 14  Amazon Web Services      -$127.43 [Sparkry ▾] [Office ▾] ✓│
│ ☐  Mar 13  Stripe payout          $1,245.00  [Sparkry ▾] [ConsInc▾] ✓│
│ ☐  Mar 12  Vercel Inc.              -$20.00  [Sparkry ▾] [Office ▾] ✓│
│                                                                       │
│ ──── Low Confidence (2) ──────────────────────────────────────────── │
│ ☐  Mar 11  Unknown - Receipt.jpg      $NaN   [       ▾] [       ▾] 📷│
│ ☐  Mar 10  Wire Transfer          $33,000.00 [       ▾] [       ▾] ✓│
│                                                                       │
│            [Batch Confirm (0 selected)]     [Comfortable | Compact]   │
└──────────────────────────────────────────────────────────────────────┘
```

### Tax Page with Per-Month B&O Breakdown

```
┌──────────────────────────────────────────────────────────────────────┐
│  Tax Summary                                          [2025 ▾]       │
│  IRS line-item breakdown, B&O totals, and export                     │
│                                                                       │
│  [Sparkry AI]  [BlackLine MTB]  [Personal]                           │
│  ─────────────────────────────────────────                           │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │  92% ready   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━░░░                │ │
│  │  220 of 240 transactions confirmed                              │ │
│  │  Review 20 unconfirmed items →                                  │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  B&O Revenue Subtotals (Monthly)                                     │
│  ┌─────────────────────────────────┐                                 │
│  │ Month        │ Income           │                                 │
│  │──────────────┼──────────────────│                                 │
│  │ Jan 2025     │     $10,000.00   │                                 │
│  │ Feb 2025     │     $10,000.00   │                                 │
│  │ Mar 2025     │     $43,000.00   │  ← Cardinal + Stripe           │
│  │ Apr 2025     │     $10,000.00   │                                 │
│  │ ...          │          ...     │                                 │
│  │──────────────┼──────────────────│                                 │
│  │ Total        │    $120,000.00   │                                 │
│  └─────────────────────────────────┘                                 │
│                                                                       │
│  [Download FreeTaxUSA]  [Download B&O Report]                        │
└──────────────────────────────────────────────────────────────────────┘
```

### Health Page with Source Configuration Guidance

```
┌──────────────────────────────────────────────────────────────────────┐
│  System Health                                    Last refreshed 5s  │
│                                                                       │
│  Source Freshness                                                     │
│  ┌───────────────────────┐  ┌───────────────────────┐                │
│  │ ● Gmail / n8n         │  │ ○ Stripe              │                │
│  │   Fresh (2h ago)      │  │   Setup Required       │                │
│  │   142 records         │  │                        │                │
│  │   [Sync Now]          │  │   Add to .env:         │                │
│  └───────────────────────┘  │   STRIPE_API_KEY_SPARKRY│               │
│  ┌───────────────────────┐  │   STRIPE_API_KEY_BLACKLINE             │
│  │ ○ Shopify             │  │                        │                │
│  │   Setup Required       │  │   [Test Connection]    │                │
│  │                        │  └───────────────────────┘                │
│  │   Add to .env:         │  ┌───────────────────────┐                │
│  │   SHOPIFY_API_KEY      │  │ → Bank CSV            │                │
│  │   SHOPIFY_STORE_URL    │  │   Import via           │                │
│  │                        │  │   Reconciliation page  │                │
│  │   [Test Connection]    │  │   [Go to Reconcile →]  │                │
│  └───────────────────────┘  └───────────────────────┘                │
│                                                                       │
│  Classification Stats        Claude API Usage                        │
│  ┌───────────────────────┐  ┌───────────────────────┐                │
│  │ Auto-confirmed: 68%   │  │ Calls this month: 47  │                │
│  │ Edited:         12%   │  │ Tokens: 143k          │                │
│  │ Pending:         8%   │  │ Est. cost: $0.05      │                │
│  │ Rejected:       12%   │  │ ████░░ $0.05          │                │
│  └───────────────────────┘  └───────────────────────┘                │
│                                                                       │
│  Tax Deadlines                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ Apr 15  Q1 Estimated Tax (1040-ES)            28 days          │ │
│  │ Apr 25  Sparkry B&O (March)                   38 days          │ │
│  │ Jun 15  Q2 Estimated Tax (1040-ES)            89 days          │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

### Register with Working Balance Column

```
┌──────────────────────────────────────────────────────────────────────┐
│  Register                                          [Export CSV]       │
│  240 transactions                                                    │
│                                                                       │
│  Income: $120,000  Expenses: -$45,000  Net: $75,000  Txns: 240      │
│                                                                       │
│  [Search...]  [All entities▾]  [All statuses▾]  [Date range▾]       │
│               ☐ Show rejected                                        │
│                                                                       │
│  Date ↓     Vendor              Category       Amount    Entity  Stat Balance│
│  ─────────────────────────────────────────────────────────────────── │
│  Mar 15    Anthropic, PBC       Office Exp.   -$42.50  Sparkry  ✓   $74,957│
│  Mar 14    EcoEnclose           COGS          -$89.00  BLine    ✓   $75,000│
│  Mar 14    Amazon               Office Exp.  -$127.43  Sparkry  ✓   $75,089│
│  Mar 13    Stripe payout        Consult Inc $1,245.00  Sparkry  ✓   $75,216│
│  Mar 12    Vercel Inc.          Office Exp.   -$20.00  Sparkry  ✓   $73,971│
│  ─── rejected (5 hidden) ───                                        │
│                                                                       │
│  ◄ Previous   Page 1 of 5   Next ►                    Show [50 ▾]   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 5. Implementation Strategy

### Phase 1: Critical Bug Fixes (P0) — 1-2 days

1. **REQ-IMP-001 + REQ-IMP-002**: Fix amount parsing in Svelte together — both require the same `parseTransaction()` transform in `api.ts`. Implement as a single change to avoid rework.
3. **REQ-IMP-003**: Fix seed_customers.py paid_date
4. **REQ-IMP-004**: Fix nextMonth() to query last invoiced month
5. **REQ-IMP-005 + REQ-IMP-013**: Add monthly_income to tax-summary API, update Svelte
6. **REQ-IMP-006**: Wire SAP checklist to API persistence

### Phase 2: Tax Compliance (P1) — 2-3 days

7. **REQ-IMP-010**: Add missing TaxCategory enum values + IRS mappings
8. **REQ-IMP-011**: 1099 tracking (raw_data approach first)
9. **REQ-IMP-012**: Estimated tax tracking section
10. **REQ-IMP-007**: Invoice-to-income transaction automation

### Phase 3: UX Workflow (P1) — 2-3 days

11. **REQ-IMP-014**: Compact review queue mode
12. **REQ-IMP-025**: Batch confirm endpoint and UI
13. **REQ-IMP-016**: Health page source config guidance
14. **REQ-IMP-008**: Stripe/Shopify onboarding flow
15. **REQ-IMP-009 + REQ-IMP-024**: Bank CSV import UI

### Phase 4: Polish (P2) — as time permits

16. **REQ-IMP-015**: Priority grouping headers
17. **REQ-IMP-017**: Rejected hidden by default
18. **REQ-IMP-018**: Filing year default
19. **REQ-IMP-019**: AR aging widget
20. **REQ-IMP-020**: Duplicate invoice warning
21. **REQ-IMP-021**: Year-over-year comparison
22. **REQ-IMP-022**: Tax year locking
23. **REQ-IMP-023**: Claude API cost tracking
24. **REQ-IMP-026**: Keyboard shortcut overlay

---

## 6. Key Design Decisions

### Amount String-to-Number: Where to fix?

**Decision**: Fix on the frontend (Svelte) by parsing amounts on fetch, not by changing the API.

**Rationale**: The API returns amounts as strings to preserve Decimal precision. Changing the API to return numbers could introduce floating-point errors for large amounts. The frontend should `parseFloat()` when it needs to do arithmetic and use the string for display.

**Implementation**: In `dashboard/src/lib/api.ts`, add a transform function that parses string amounts to numbers in the TransactionList response:

```typescript
function parseTransaction(tx: any): Transaction {
  return {
    ...tx,
    amount: tx.amount != null ? parseFloat(tx.amount) : null
  };
}
```

### B&O Monthly Breakdown: Separate endpoint or inline?

**Decision**: Inline in `/api/tax-summary` response.

**Rationale**: The B&O breakdown is always shown on the tax page alongside the IRS breakdown. A separate endpoint would require an additional fetch. The data is small (12 or 4 numbers) and cheap to compute.

### 1099 Tracking: New column or raw_data?

**Decision**: Start with `raw_data` JSON field, migrate to dedicated column later if needed.

**Rationale**: 1099 tracking is a P1 feature but may evolve. Using raw_data avoids a migration and allows flexible schema. The tax export can query by JSON field. If reporting needs become complex, add `form_1099_source` and `form_1099_ein` columns.

### Invoice-to-Income Automation: Auto-create or prompt?

**Decision**: Auto-create the income transaction when invoice is marked paid, with a toast notification.

**Rationale**: Travis doesn't want extra steps. The transaction is always needed (paid invoice = income received). The toast provides awareness without requiring action. If the auto-created transaction needs adjustment, Travis can edit it in the register.

### SAP Checklist Persistence: Debounce or immediate?

**Decision**: Immediate PATCH on each checkbox toggle, with optimistic UI update.

**Rationale**: The checklist has only 8 items, so at most 8 API calls per invoice. Debouncing could lose state if the user navigates away quickly. Optimistic update means the UI feels instant. If the PATCH fails, revert the checkbox and show an error toast.

**Concurrency note**: If two tabs edit the same checklist, last-write-wins. This is acceptable for a single-user system. The PATCH sends the full checklist state (not a delta), so there is no merge conflict — just potential overwrite.

### Invoice-to-Income Idempotency

**Decision**: Check `payment_transaction_id` before creating an income transaction.

**Rationale**: The mark-paid endpoint must be idempotent. If called twice (e.g., due to network retry), the second call should not create a duplicate income transaction. The guard is: `if invoice.payment_transaction_id is not None: skip creation, return existing`. This is implemented as a simple DB check before INSERT, not as a UNIQUE constraint (since the same transaction could theoretically be linked to different invoices if voided and re-paid).

### Tax Year Lock Performance

**Decision**: Cache lock status per request, not per transaction.

**Rationale**: Checking tax year locks adds a DB query to PATCH /transactions/{id}. For single edits, this is negligible. For bulk-confirm (up to 100 items), we should query all relevant locks once at the start of the batch (e.g., `SELECT * FROM tax_year_locks WHERE entity = ? AND year IN (?)`) and check in-memory per item. This avoids N+1 queries.

### Estimated Tax Calculation

**Decision**: Simple formula using self-employment tax rate as a rough estimate.

**Formula**: `estimated_annual = net_profit * 0.25` (approximation covering ~15.3% SE tax + ~10% federal income tax for the relevant bracket). This is a planning tool, not a tax filing tool. Travis should consult his actual tax situation.

**Rationale**: The system cannot know Travis's full tax picture (other income, deductions, credits). A rough estimate is more useful than no estimate. The UI should clearly label it as "Estimated (approximate)".
