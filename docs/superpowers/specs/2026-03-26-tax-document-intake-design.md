# Tax Document Intake System — Design Spec

> Ingest, catalog, validate, and export received tax documents (1099s, K-1s, 1098s, property tax statements) to streamline tax filing.

## Problem

Tax season requires collecting documents from multiple sources (employers, banks, brokerages, county assessors), manually tracking what's arrived, and transcribing amounts into tax software. The accounting system handles transaction-level data well but has no concept of the summary-level tax documents the IRS uses for cross-referencing.

## Scope

### In scope
- New `TaxDocument` SQLAlchemy model and migration
- `/tax-doc` Claude Code skill for PDF intake with validation
- Python ingest helper (`src/tax_docs/ingest.py`) with insert + read-back verification
- Filing-ready summary report grouped by entity with IRS line mappings
- Light reconciliation (compare 1099 totals vs. transaction register)
- FreeTaxUSA/TaxAct export enhancement to include tax document data
- Dashboard page for viewing/managing tax documents
- API endpoints for CRUD + summary

### Out of scope
- Automated PDF parsing (Claude reads the PDF in-conversation)
- Outbound 1099 generation (issuing 1099s to contractors)
- Full reconciliation engine
- IRS e-file integration

## Entities

Tax documents are assigned to one of the three existing entities:

| Entity | Relevant Forms |
|---|---|
| Personal | 1099-NEC (freelance), 1099-INT, 1099-DIV, 1099-B, 1098, Property Tax |
| Sparkry AI LLC | 1099-NEC (Cardinal Health), 1099-K, K-1 (from BlackLine) |
| BlackLine MTB LLC | 1099-K, potentially 1099-NEC |

---

## Data Model

### Enums (added to `src/models/enums.py`)

```python
class TaxFormType(enum.StrEnum):
    """IRS form types for received tax documents."""
    FORM_1099_NEC = "1099-NEC"
    FORM_1099_INT = "1099-INT"
    FORM_1099_DIV = "1099-DIV"
    FORM_1099_B = "1099-B"
    FORM_1099_K = "1099-K"
    FORM_K1 = "K-1"
    FORM_1098 = "1098"
    PROPERTY_TAX = "PROPERTY_TAX"
    OTHER = "OTHER"

class TaxDocumentStatus(enum.StrEnum):
    """Lifecycle status of a tax document record."""
    ACTIVE = "active"
    INACTIVE = "inactive"  # soft delete
```

Note: `W-2` is excluded — Travis has no employment income. If needed later, add to the enum.

### `TaxDocument` model (in `src/models/tax_document.py`, registered in `src/models/__init__.py`)

Follows existing codebase conventions: model lives in `src/models/`, uses `StrEnum` + `CheckConstraint` for constrained columns, registered in `__init__.py` so Alembic autogenerate detects it.

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | TEXT (UUID) | PK | Primary key |
| `tax_year` | INTEGER | NOT NULL, INDEX | e.g., 2025 |
| `form_type` | TEXT(16) | NOT NULL, CHECK (TaxFormType enum) | Validated against `TaxFormType` enum |
| `entity` | TEXT(16) | NOT NULL, CHECK (Entity enum) | Reuses existing `Entity` enum |
| `payer_name` | TEXT(255) | NOT NULL | e.g., "FC International Education LLC" |
| `payer_ein` | TEXT(10) | nullable | e.g., "85-1499443" |
| `recipient_name` | TEXT(255) | NOT NULL | e.g., "Travis Sparks" |
| `recipient_tin_last4` | TEXT(4) | nullable | Last 4 of SSN/EIN for verification |
| `amounts` | JSON | NOT NULL | Form-specific box values (see below) |
| `total_amount` | NUMERIC(12,2) | NOT NULL | Primary/headline amount (see `total_amount` mapping below) |
| `source_file` | TEXT | nullable | Relative path from project root, e.g., `data/tax-docs/2025/personal/...` |
| `notes` | TEXT | nullable | Free-form notes |
| `status` | TEXT(16) | NOT NULL, CHECK (TaxDocumentStatus enum), default `active` | Validated against `TaxDocumentStatus` enum |
| `created_at` | DATETIME | NOT NULL | When ingested |
| `updated_at` | DATETIME | NOT NULL | Last modified |

**Unique constraint:** `UNIQUE(tax_year, form_type, entity, payer_ein)` — prevents duplicate ingestion of the same form. For payers without EIN (property tax), dedup uses `UNIQUE(tax_year, form_type, entity, payer_name)` as a soft check in `ingest_and_verify()`.

**Tax year lock:** Mutations (POST, PATCH, DELETE) check `TaxYearLock` for the target entity+year. If locked, the operation is rejected with 409 Conflict, matching the existing transaction lock behavior.

### `total_amount` mapping by form type

| Form Type | `total_amount` source |
|---|---|
| 1099-NEC | `box_1_nonemployee_comp` |
| 1099-INT | `box_1_interest` |
| 1099-DIV | `box_1a_ordinary_dividends` |
| 1099-B | `gain_loss` (net proceeds minus cost basis) |
| 1099-K | `box_1a_gross_amount` |
| K-1 | `box_1_ordinary_income` |
| 1098 | `box_1_mortgage_interest` |
| PROPERTY_TAX | `tax_amount` |

### `amounts` JSON structure by form type

Each form type has a defined set of expected keys. The ingest helper validates that the submitted JSON contains only keys valid for the given `form_type` and that the primary box (used for `total_amount`) is present. Unknown keys are rejected at the Pydantic validation layer.

```json
// 1099-NEC
{"box_1_nonemployee_comp": 1781.62, "box_4_federal_tax_withheld": 0}

// 1099-INT
{"box_1_interest": 342.18, "box_3_savings_bond_interest": 0, "box_4_federal_tax_withheld": 0}

// 1099-DIV
{"box_1a_ordinary_dividends": 1205.00, "box_1b_qualified_dividends": 980.00, "box_2a_capital_gain": 0}

// 1099-B
{"proceeds": 15000.00, "cost_basis": 12000.00, "gain_loss": 3000.00, "short_term_count": 5, "long_term_count": 12}

// 1099-K
{"box_1a_gross_amount": 48000.00, "box_1b_card_not_present": 48000.00}

// K-1
{"box_1_ordinary_income": 12000.00, "box_14_se_earnings": 12000.00, "box_16_foreign_transactions": 0}

// 1098
{"box_1_mortgage_interest": 8412.00, "box_2_outstanding_principal": 320000.00, "box_5_property_tax": 6200.00}

// PROPERTY_TAX
{"assessed_value": 850000.00, "tax_amount": 6200.00, "year": 2025}
```

### Amounts validation

A `VALID_AMOUNT_KEYS` dict maps each `TaxFormType` to its allowed JSON keys plus the required primary key. Validation is implemented as a Pydantic validator on `TaxDocumentCreate`:

```python
VALID_AMOUNT_KEYS: dict[str, tuple[set[str], str]] = {
    "1099-NEC": ({"box_1_nonemployee_comp", "box_4_federal_tax_withheld"}, "box_1_nonemployee_comp"),
    "1099-INT": ({"box_1_interest", "box_3_savings_bond_interest", "box_4_federal_tax_withheld"}, "box_1_interest"),
    # ... etc for each form type
}
```

---

## `/tax-doc` Skill

### Trigger
`/tax-doc /path/to/file.pdf` or `/tax-doc` (then provide path)

### Flow

**Phase 1: Extract & Confirm**
1. Read the PDF using Claude's document understanding
2. Extract: form type, tax year, payer name, payer EIN, recipient, amounts per box
3. Guess entity assignment based on recipient name and form context
4. Display structured summary table for user confirmation
5. User confirms or corrects (especially entity assignment)

**Phase 2: Commit & Validate**
1. Copy PDF to `data/tax-docs/{year}/{entity}/{descriptive-filename}.pdf`
2. Call `ingest_and_verify()` — inserts row, reads it back, compares
3. Print validation checklist:

```
Tax Document Ingestion — Validation
────────────────────────────────────
[x] PDF copied to data/tax-docs/2025/personal/1099-nec-fc-international-education.pdf
[x] DB row inserted: id=abc12345
[x] form_type: 1099-NEC
[x] payer: FC International Education LLC
[x] EIN: 85-1499443
[x] total_amount: $1,781.62
[x] entity: personal
[x] tax_year: 2025
[x] amounts JSON has expected boxes
```

4. If any mismatch, flag it immediately for correction

---

## Python Helper: `src/tax_docs/ingest.py`

### Public API

```python
def ingest_and_verify(doc: TaxDocumentCreate, db: Session) -> IngestResult:
    """Insert a TaxDocument row and read it back for verification.

    Returns IngestResult with pass/fail status and checklist items.
    """

def list_documents(db: Session, year: int, entity: str | None = None) -> list[TaxDocument]:
    """List tax documents, optionally filtered by entity."""

def get_summary(db: Session, year: int) -> TaxSummaryReport:
    """Generate filing-ready summary grouped by entity with IRS line mappings."""

def reconcile_light(db: Session, year: int) -> list[ReconciliationFlag]:
    """Compare tax document totals against transaction register. Flag differences > $1."""
```

### IngestResult

```python
@dataclass
class IngestResult:
    success: bool
    document_id: str
    checklist: list[ChecklistItem]  # Each has: label, expected, actual, passed

@dataclass
class ChecklistItem:
    label: str
    expected: str
    actual: str
    passed: bool
```

---

## Tax Summary Report

### Output format

```
═══ 2025 Tax Documents — Personal ═══

Form        Payer                              Amount        IRS Line
─────────── ────────────────────────────────── ──────────── ─────────────────────
1099-NEC    FC International Education LLC      $1,781.62   Schedule C / Line 1
1099-INT    Chase Bank                            $342.18   Schedule B / Line 1
1099-DIV    Schwab Brokerage                    $1,205.00   Schedule B / Line 5
1099-B      Schwab Brokerage                    (see CSV)   Schedule D / 8949
1098        US Bank                             $8,412.00   Schedule A / Line 8a
Prop Tax    King County Assessor                $6,200.00   Schedule A / SALT

═══ 2025 Tax Documents — Sparkry AI LLC ═══
...
```

### IRS Line Mapping

| Form Type | Primary Box | IRS Line |
|---|---|---|
| 1099-NEC | box_1 | Schedule C Line 1 (if SE) or 1040 Line 8 (if not SE) |
| 1099-INT | box_1 | Schedule B Line 1 |
| 1099-DIV | box_1a | Schedule B Line 5 |
| 1099-B | proceeds/basis | Schedule D + Form 8949 |
| 1099-K | box_1a | Schedule C Line 1 (business) or 1040 (personal) |
| K-1 | box_1 | Schedule E Part II |
| 1098 | box_1 | Schedule A Line 8a |
| PROPERTY_TAX | tax_amount | Schedule A / SALT (subject to $10k cap) |

### Light Reconciliation

When generating the summary, for each 1099-type document:
1. First try matching via the existing `payer_1099` field on transactions (exact match on payer name + form type)
2. Fall back to fuzzy matching on transaction `description` vs. `payer_name` (case-insensitive contains), filtered by entity + tax year date range
3. Sum matched transaction amounts
4. If `abs(tax_doc_total - transaction_sum) > 1.00`, flag:

```
⚠ 1099-NEC Cardinal Health: $198,000.00 reported, $196,500.00 in register (diff: $1,500.00)
```

This is informational, not blocking.

---

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/tax-documents?year=2025&entity=personal` | List documents, filterable |
| `GET` | `/api/tax-documents/summary?year=2025` | Filing-ready summary report |
| `GET` | `/api/tax-documents/{id}` | Single document detail |
| `POST` | `/api/tax-documents` | Create new document |
| `PATCH` | `/api/tax-documents/{id}` | Partial update (matches existing `PATCH` convention on transactions) |
| `DELETE` | `/api/tax-documents/{id}` | Soft delete (set status=inactive) |

All endpoints follow existing FastAPI patterns: import `get_db` from `src/api/deps.py`, auth via API key, JSON responses, standard error handling. All mutation endpoints (POST, PATCH, DELETE) check `TaxYearLock` before proceeding.

---

## Tax Export Enhancement

### FreeTaxUSA (`src/export/freetaxusa.py`)

Add a new section to the summary output that lists all tax documents with their IRS line mappings. Pull amounts from `TaxDocument.amounts` JSON for specific box values rather than just `total_amount`.

### TaxAct (`src/export/taxact.py`)

Same enhancement — include tax document data in the print-friendly summary.

### New: `generate_tax_doc_summary()` in `src/export/tax_doc_report.py`

Standalone function that produces the filing-ready summary report. Called by the API endpoint and can be used independently.

---

## Dashboard Page

### Route: `/tax-documents`

**Table view:**
- Grouped by entity (Personal, Sparkry, BlackLine)
- Columns: Form Type, Payer, Amount, Tax Year, Status, IRS Line
- Click row to see full detail (all box amounts, PDF link, notes)
- "Download Summary" button → exports the filing-ready report

**Expected vs. Received tracking:**
- Ability to mark payers as "expected" (e.g., Cardinal Health always sends a 1099-NEC)
- Visual indicator: received (green check) vs. expected but not yet received (orange clock)
- This is a nice-to-have for v2

### API integration
- Uses the same FastAPI endpoints defined above
- Follows existing SvelteKit dashboard patterns (keyboard navigation, Apple design)

---

## File Layout

```
src/
  models/
    tax_document.py     # TaxDocument SQLAlchemy model (registered in __init__.py)
  tax_docs/
    __init__.py
    ingest.py           # ingest_and_verify(), list, summary, reconcile
    test_ingest.py      # Co-located tests
  api/
    routes/
      tax_documents.py  # FastAPI CRUD + summary endpoints (uses get_db from src/api/deps.py)
  export/
    tax_doc_report.py   # Filing-ready summary generator

data/
  tax-docs/
    2025/
      personal/         # PDFs for personal entity
      sparkry/          # PDFs for Sparkry
      blackline/        # PDFs for BlackLine

skills/
  tax-doc/
    SKILL.md            # Claude Code skill definition
```

---

## Build Order

1. **Model + Migration** — `TaxDocument` SQLAlchemy model, Alembic migration
2. **Ingest helper** — `ingest_and_verify()` with validation checklist
3. **`/tax-doc` skill** — Claude Code skill for PDF intake
4. **Summary report** — `generate_tax_doc_summary()` with IRS line mappings
5. **Light reconciliation** — compare against transaction register
6. **API endpoints** — CRUD + summary
7. **Export enhancement** — FreeTaxUSA/TaxAct integration
8. **Dashboard page** — SvelteKit UI (lower priority)

---

## Testing

- Test `ingest_and_verify()` with known fixture data for each form type
- Test summary report output format
- Test reconciliation flagging with intentional mismatches
- Test API endpoints (create, read, update, soft delete)
- Test amounts JSON validation per form type
- Test PDF copy to correct directory structure
