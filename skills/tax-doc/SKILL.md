---
name: tax-doc
description: Ingest a received tax document (1099, K-1, 1098, property tax) from a PDF — extract, confirm, copy, and store in the DB
user_invocable: true
---

# TAX-DOC — Tax Document Intake

## Trigger

```
/tax-doc /path/to/file.pdf
```

Or run `/tax-doc` and provide the path when prompted.

## Context

- Project root: `/Users/travis/SGDrive/dev/accounting`
- Database: `data/accounting.db`
- PDF storage: `data/tax-docs/{year}/{entity}/`
- Three entities: `personal`, `sparkry`, `blackline`

---

## Phase 1: Extract & Confirm

### 1. Read the PDF

Use Claude's document understanding to read the PDF provided as the argument.

### 2. Extract all fields

Extract the following from the document:

| Field | Description |
|---|---|
| `form_type` | One of: `1099-NEC`, `1099-INT`, `1099-DIV`, `1099-B`, `1099-K`, `K-1`, `1098`, `PROPERTY_TAX` |
| `tax_year` | Integer (e.g., `2025`) |
| `payer_name` | Full legal name of the payer/issuer |
| `payer_ein` | EIN in `XX-XXXXXXX` format (nullable) |
| `recipient_name` | Name as shown on the form |
| `recipient_tin_last4` | Last 4 digits of SSN or EIN (nullable) |
| `amounts` | JSON object — all box values per form type (see schema below) |
| `total_amount` | Primary headline amount (see `total_amount` mapping below) |

### 3. Determine entity assignment

Assign `entity` based on recipient name and form context:

| Signal | Entity |
|---|---|
| Recipient is "Travis Sparks" (person) | `personal` |
| Recipient is "Sparkry AI LLC" or "Sparkry" | `sparkry` |
| Recipient is "BlackLine MTB LLC" or "BlackLine" | `blackline` |
| Payer is "Cardinal Health" (1099-NEC) | `sparkry` |
| K-1 from a partnership (issued TO Sparkry) | `sparkry` |
| Home mortgage interest (1098) | `personal` |
| Property tax statement | `personal` |

If uncertain, default to `personal` and flag for confirmation.

### 4. Display summary table for confirmation

Present the extracted data in a structured table before proceeding:

```
Tax Document — Extracted Fields
────────────────────────────────────────────────────────
Form Type:          1099-NEC
Tax Year:           2025
Payer:              FC International Education LLC
Payer EIN:          85-1499443
Recipient:          Travis Sparks
Recipient TIN:      ***-**-XXXX (last 4)
Entity:             personal
Total Amount:       $1,781.62

Amounts (boxes):
  box_1_nonemployee_comp:    $1,781.62
  box_4_federal_tax_withheld: $0.00

Source file:        1099-nec-fc-international-education-2025.pdf
────────────────────────────────────────────────────────
Does this look correct? Confirm or correct any fields.
```

### 5. Wait for user confirmation

Do NOT proceed to Phase 2 until the user confirms. Accept corrections to any field, especially entity assignment and amounts.

---

## Phase 2: Commit & Validate

### 1. Copy PDF to storage directory

```
data/tax-docs/{tax_year}/{entity}/{descriptive-filename}.pdf
```

Filename convention: `{form_type_lower}-{payer_slug}-{tax_year}.pdf`
- Lowercase, hyphens for spaces
- Example: `1099-nec-fc-international-education-2025.pdf`
- Example: `1099-int-chase-bank-2025.pdf`
- Example: `1098-us-bank-2025.pdf`

Create the directory if it doesn't exist.

### 2. Call the ingest helper

```python
import sys
sys.path.insert(0, '/Users/travis/SGDrive/dev/accounting')

from src.db.connection import SessionLocal
from src.tax_docs.ingest import ingest_and_verify
from src.models.tax_document import TaxDocumentCreate

doc = TaxDocumentCreate(
    tax_year=2025,
    form_type="1099-NEC",
    entity="personal",
    payer_name="FC International Education LLC",
    payer_ein="85-1499443",
    recipient_name="Travis Sparks",
    recipient_tin_last4="1234",
    amounts={"box_1_nonemployee_comp": 1781.62, "box_4_federal_tax_withheld": 0},
    total_amount=1781.62,
    source_file="data/tax-docs/2025/personal/1099-nec-fc-international-education-2025.pdf",
    notes=None,
)

db = SessionLocal()
result = ingest_and_verify(doc, db)
db.close()
print(result)
```

If the ingest helper is not yet implemented, fall back to the API:

```bash
curl -X POST http://127.0.0.1:8000/api/tax-documents \
  -H "Content-Type: application/json" \
  -d '{ ... extracted data as JSON ... }'
```

### 3. Print validation checklist

```
Tax Document Ingestion — Validation
────────────────────────────────────
[x] PDF copied to data/tax-docs/2025/personal/1099-nec-fc-international-education-2025.pdf
[x] DB row inserted: id=abc12345
[x] form_type: 1099-NEC
[x] payer: FC International Education LLC
[x] EIN: 85-1499443
[x] total_amount: $1,781.62
[x] entity: personal
[x] tax_year: 2025
[x] amounts JSON has expected boxes
```

If any checklist item fails, flag it immediately with the mismatch details and do not proceed silently.

---

## Amounts JSON Schema by Form Type

### 1099-NEC
```json
{
  "box_1_nonemployee_comp": 1781.62,
  "box_4_federal_tax_withheld": 0
}
```
**`total_amount` source:** `box_1_nonemployee_comp`

### 1099-INT
```json
{
  "box_1_interest": 342.18,
  "box_3_savings_bond_interest": 0,
  "box_4_federal_tax_withheld": 0
}
```
**`total_amount` source:** `box_1_interest`

### 1099-DIV
```json
{
  "box_1a_ordinary_dividends": 1205.00,
  "box_1b_qualified_dividends": 980.00,
  "box_2a_capital_gain": 0
}
```
**`total_amount` source:** `box_1a_ordinary_dividends`

### 1099-B
```json
{
  "proceeds": 15000.00,
  "cost_basis": 12000.00,
  "gain_loss": 3000.00,
  "short_term_count": 5,
  "long_term_count": 12
}
```
**`total_amount` source:** `gain_loss` (proceeds minus cost basis)

### 1099-K
```json
{
  "box_1a_gross_amount": 48000.00,
  "box_1b_card_not_present": 48000.00
}
```
**`total_amount` source:** `box_1a_gross_amount`

### K-1
```json
{
  "box_1_ordinary_income": 12000.00,
  "box_14_se_earnings": 12000.00,
  "box_16_foreign_transactions": 0
}
```
**`total_amount` source:** `box_1_ordinary_income`

### 1098 (Mortgage Interest)
```json
{
  "box_1_mortgage_interest": 8412.00,
  "box_2_outstanding_principal": 320000.00,
  "box_5_property_tax": 6200.00
}
```
**`total_amount` source:** `box_1_mortgage_interest`

### PROPERTY_TAX
```json
{
  "assessed_value": 850000.00,
  "tax_amount": 6200.00,
  "year": 2025
}
```
**`total_amount` source:** `tax_amount`

---

## `total_amount` Mapping Summary

| Form Type | `total_amount` field |
|---|---|
| 1099-NEC | `box_1_nonemployee_comp` |
| 1099-INT | `box_1_interest` |
| 1099-DIV | `box_1a_ordinary_dividends` |
| 1099-B | `gain_loss` |
| 1099-K | `box_1a_gross_amount` |
| K-1 | `box_1_ordinary_income` |
| 1098 | `box_1_mortgage_interest` |
| PROPERTY_TAX | `tax_amount` |

---

## Entity Assignment Reference

| Recipient / Payer | Entity |
|---|---|
| "Travis Sparks" (personal name) | `personal` |
| "Sparkry AI LLC", "Sparkry" | `sparkry` |
| "BlackLine MTB LLC", "BlackLine" | `blackline` |
| Cardinal Health (payer on 1099-NEC) | `sparkry` |
| Home mortgage / property at personal address | `personal` |
| K-1 issued to Sparkry from a partnership | `sparkry` |

When in doubt, ask the user before defaulting.

---

## IRS Line Mappings (for reference)

| Form Type | IRS Destination |
|---|---|
| 1099-NEC | Schedule C Line 1 (self-employment) or 1040 Line 8 |
| 1099-INT | Schedule B Line 1 |
| 1099-DIV | Schedule B Line 5 |
| 1099-B | Schedule D + Form 8949 |
| 1099-K | Schedule C Line 1 (business) or 1040 (personal) |
| K-1 | Schedule E Part II |
| 1098 | Schedule A Line 8a |
| PROPERTY_TAX | Schedule A / SALT (subject to $10,000 cap) |

---

## Error Handling

- **Duplicate detected** (same `tax_year + form_type + entity + payer_ein`): Show the existing record and ask the user whether to update or skip.
- **Missing primary box**: Flag which box is missing and ask the user to provide the value before continuing.
- **Tax year locked**: If the target year+entity is locked, report the 409 and ask the user if they want to proceed with an unlock.
- **PDF not found**: Confirm the path and ask the user to re-provide it.
