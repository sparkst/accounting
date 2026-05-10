---
name: tax-doc
description: Ingest a received tax document (1099-NEC, 1099-INT, 1099-DIV, 1099-B, 1099-K, K-1, 1098, property tax statement) from a PDF into the accounting database. Use this skill whenever the user shares a tax form, mentions receiving a 1099 or other tax document, drops a PDF path, or says anything like "here's my 1099", "got a tax form", "file this tax doc", or "add this to my tax documents". Also trigger when the user asks to see a summary of received tax documents or wants to check what tax forms have been filed.
user_invocable: true
---

# Tax Document Intake

Ingest a tax document PDF: extract structured data, confirm with the user, copy the file, and store in the database with verification.

## Phase 1: Extract & Confirm

### Step 1 — Read the PDF

Read the PDF the user provided. They may give a file path (use the Read tool) or paste/attach it directly in conversation.

### Step 2 — Extract fields

Pull these fields from the document:

| Field | What to look for |
|---|---|
| `form_type` | `1099-NEC`, `1099-INT`, `1099-DIV`, `1099-B`, `1099-K`, `K-1`, `1098`, or `PROPERTY_TAX` |
| `tax_year` | Calendar year on the form |
| `payer_name` | Full legal name of the issuer |
| `payer_ein` | EIN (XX-XXXXXXX format), or `None` if not shown |
| `recipient_name` | Recipient name as printed |
| `recipient_tin_last4` | Last 4 of SSN/EIN, or `None` |
| `amounts` | All box values — use the exact keys from the schema below |
| `total_amount` | The primary box value (see mapping below) |

### Step 3 — Assign entity

| Signal | Entity |
|---|---|
| Recipient is "Travis Sparks" | `personal` |
| Recipient is "Sparkry AI LLC" | `sparkry` |
| Recipient is "BlackLine MTB LLC" | `blackline` |
| Payer is "Cardinal Health" | `sparkry` |
| K-1 issued TO Sparkry | `sparkry` |
| 1098 or property tax | `personal` |

If unclear, ask the user.

### Step 4 — Show summary and wait for confirmation

```
Tax Document — Extracted Fields
────────────────────────────────────────
Form Type:     1099-NEC
Tax Year:      2025
Payer:         FC International Education LLC
Payer EIN:     85-1499443
Recipient:     Travis Sparks
Entity:        personal
Total Amount:  $1,781.62

Amounts:
  box_1_nonemployee_comp:      $1,781.62
  box_4_federal_tax_withheld:  $0.00
────────────────────────────────────────
Does this look correct? (confirm or correct any fields)
```

Do NOT proceed until the user confirms. Accept corrections to any field.

---

## Phase 2: Commit & Validate

### Step 1 — Copy PDF

```bash
mkdir -p data/tax-docs/{tax_year}/{entity}
cp "{source_path}" "data/tax-docs/{tax_year}/{entity}/{form_type_lower}-{payer_slug}-{tax_year}.pdf"
```

Filename: lowercase, hyphens for spaces. Example: `1099-nec-fc-international-education-2025.pdf`

### Step 2 — Insert and verify

Run this via the Bash tool. Replace all values with the confirmed data from Phase 1.

```bash
cd /Users/travis/SGDrive/dev/accounting && source .venv/bin/activate && python3 << 'PYEOF'
from src.db.connection import SessionLocal
from src.tax_docs.ingest import ingest_and_verify

doc_data = {
    "tax_year": 2025,
    "form_type": "1099-NEC",
    "entity": "personal",
    "payer_name": "FC International Education LLC",
    "payer_ein": "85-1499443",
    "recipient_name": "Travis Sparks",
    "recipient_tin_last4": "8950",
    "amounts": {"box_1_nonemployee_comp": 1781.62, "box_4_federal_tax_withheld": 0},
    "total_amount": 1781.62,
    "source_file": "data/tax-docs/2025/personal/1099-nec-fc-international-education-2025.pdf",
    "notes": None,
}

db = SessionLocal()
try:
    result = ingest_and_verify(doc_data, db)
    print(f"Success: {result.success}")
    print(f"Document ID: {result.document_id}")
    for item in result.checklist:
        mark = "[x]" if item.passed else "[ ]"
        print(f"  {mark} {item.label}: expected={item.expected}, actual={item.actual}")
    if result.error:
        print(f"ERROR: {result.error}")
finally:
    db.close()
PYEOF
```

The example above is for a 1099-NEC — replace all values with the actual confirmed data. For `payer_ein` or `recipient_tin_last4` that are missing, use `None` (Python None, not a string).

### Step 3 — Report results

Show the validation checklist from the script output. Every item should show `[x]`. If any show `[ ]`, flag the mismatch and investigate before telling the user it succeeded.

---

## Amounts Schema

Each form type has specific box keys. Only use keys from this table — unknown keys will be rejected.

| Form Type | Amounts keys | `total_amount` from |
|---|---|---|
| `1099-NEC` | `box_1_nonemployee_comp`, `box_4_federal_tax_withheld` | `box_1_nonemployee_comp` |
| `1099-INT` | `box_1_interest`, `box_3_savings_bond_interest`, `box_4_federal_tax_withheld` | `box_1_interest` |
| `1099-DIV` | `box_1a_ordinary_dividends`, `box_1b_qualified_dividends`, `box_2a_capital_gain` | `box_1a_ordinary_dividends` |
| `1099-B` | `proceeds`, `cost_basis`, `gain_loss`, `short_term_count`, `long_term_count` | `gain_loss` |
| `1099-K` | `box_1a_gross_amount`, `box_1b_card_not_present` | `box_1a_gross_amount` |
| `K-1` | `box_1_ordinary_income`, `box_14_se_earnings`, `box_16_foreign_transactions` | `box_1_ordinary_income` |
| `1098` | `box_1_mortgage_interest`, `box_2_outstanding_principal`, `box_5_property_tax` | `box_1_mortgage_interest` |
| `PROPERTY_TAX` | `assessed_value`, `tax_amount`, `year` | `tax_amount` |

Only include boxes that have values on the form. The primary box (rightmost column) is required.

## IRS Line Reference

| Form | Goes to |
|---|---|
| 1099-NEC | Schedule C Line 1 or 1040 Line 8 |
| 1099-INT | Schedule B Line 1 |
| 1099-DIV | Schedule B Line 5 |
| 1099-B | Schedule D + Form 8949 |
| 1099-K | Schedule C Line 1 (business) or 1040 (personal) |
| K-1 | Schedule E Part II |
| 1098 | Schedule A Line 8a |
| PROPERTY_TAX | Schedule A / SALT ($10k cap) |

## Error Handling

- **Duplicate**: Show existing record, ask user to update or skip
- **Missing primary box**: Ask user for the value before inserting
- **Tax year locked**: Report and ask if user wants to unlock
- **PDF not found**: Ask user to re-provide the path
