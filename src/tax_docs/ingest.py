"""Tax document ingest helper.

Provides insert + read-back verification, listing, summary, and light
reconciliation against the transaction register.

Design spec: §Python Helper — src/tax_docs/ingest.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.export.tax_doc_report import generate_tax_doc_summary
from src.models.tax_document import TaxDocument
from src.models.transaction import Transaction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VALID_AMOUNT_KEYS
# Maps TaxFormType value → (allowed_keys: set[str], required_primary_key: str)
# ---------------------------------------------------------------------------

VALID_AMOUNT_KEYS: dict[str, tuple[set[str], str]] = {
    "1099-NEC": (
        {"box_1_nonemployee_comp", "box_4_federal_tax_withheld"},
        "box_1_nonemployee_comp",
    ),
    "1099-INT": (
        {"box_1_interest", "box_3_savings_bond_interest", "box_4_federal_tax_withheld"},
        "box_1_interest",
    ),
    "1099-DIV": (
        {"box_1a_ordinary_dividends", "box_1b_qualified_dividends", "box_2a_capital_gain"},
        "box_1a_ordinary_dividends",
    ),
    "1099-B": (
        {"proceeds", "cost_basis", "gain_loss", "short_term_count", "long_term_count"},
        "gain_loss",
    ),
    "1099-K": (
        {"box_1a_gross_amount", "box_1b_card_not_present"},
        "box_1a_gross_amount",
    ),
    "K-1": (
        {"box_1_ordinary_income", "box_14_se_earnings", "box_16_foreign_transactions"},
        "box_1_ordinary_income",
    ),
    "1098": (
        {"box_1_mortgage_interest", "box_2_outstanding_principal", "box_5_property_tax"},
        "box_1_mortgage_interest",
    ),
    "PROPERTY_TAX": (
        {"assessed_value", "tax_amount", "year"},
        "tax_amount",
    ),
    "OTHER": (
        set(),  # any keys allowed
        "",     # no required primary key
    ),
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ChecklistItem:
    label: str
    expected: str
    actual: str
    passed: bool


@dataclass
class IngestResult:
    success: bool
    document_id: str
    checklist: list[ChecklistItem] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_amounts(form_type: str, amounts: dict[str, Any]) -> None:
    """Validate the amounts dict for a given form type.

    Raises ValueError if validation fails.

    Rules:
    - All keys must be in the allowed set for the form type (unknown keys rejected).
    - The required primary key must be present.
    - OTHER form type accepts any keys with no required primary.
    """
    if form_type not in VALID_AMOUNT_KEYS:
        raise ValueError(f"Unknown form_type: {form_type!r}")

    allowed_keys, required_key = VALID_AMOUNT_KEYS[form_type]

    # OTHER is open-ended
    if form_type == "OTHER":
        return

    unknown = set(amounts.keys()) - allowed_keys
    if unknown:
        raise ValueError(f"Unknown amount keys for {form_type}: {sorted(unknown)}")

    if required_key and required_key not in amounts:
        raise ValueError(f"Required key {required_key!r} missing from amounts for {form_type}")


# ---------------------------------------------------------------------------
# ingest_and_verify
# ---------------------------------------------------------------------------

def ingest_and_verify(doc_data: dict[str, Any], db: Session) -> IngestResult:
    """Insert a TaxDocument row from doc_data and read it back for verification.

    Returns IngestResult with a pass/fail checklist for each verified field.

    The following fields are compared: form_type, payer_name, payer_ein,
    total_amount, amounts, entity, tax_year.

    NULL-EIN dedup: when payer_ein is None, a soft check queries for an existing
    active document with the same (tax_year, form_type, entity, payer_name).
    If found, returns an error IngestResult without inserting.
    """
    checklist: list[ChecklistItem] = []

    # ── Amounts validation ────────────────────────────────────────────────────
    form_type = doc_data.get("form_type", "")
    amounts = doc_data.get("amounts", {})
    try:
        validate_amounts(form_type, amounts)
    except ValueError as e:
        err_msg = str(e)
        checklist.append(ChecklistItem(
            label="amounts validation",
            expected="valid",
            actual=err_msg,
            passed=False,
        ))
        return IngestResult(success=False, document_id="", checklist=checklist, error=err_msg)
    checklist.append(ChecklistItem(
        label="amounts validation",
        expected="valid",
        actual="valid",
        passed=True,
    ))

    # ── NULL-EIN soft dedup check ─────────────────────────────────────────────
    payer_ein = doc_data.get("payer_ein")
    if payer_ein is None:
        tax_year = doc_data.get("tax_year")
        entity = doc_data.get("entity", "")
        payer_name = doc_data.get("payer_name", "")
        existing = (
            db.query(TaxDocument)
            .filter(
                TaxDocument.tax_year == tax_year,
                TaxDocument.form_type == form_type,
                TaxDocument.entity == entity,
                TaxDocument.payer_name == payer_name,
                TaxDocument.status == "active",
            )
            .first()
        )
        if existing is not None:
            msg = (
                f"Duplicate: active {form_type} from {payer_name!r} "
                f"for {entity}/{tax_year} already exists (id={existing.id[:8]})"
            )
            checklist.append(ChecklistItem(
                label="NULL-EIN dedup",
                expected="no duplicate",
                actual=f"duplicate found: {existing.id[:8]}",
                passed=False,
            ))
            return IngestResult(success=False, document_id="", checklist=checklist, error=msg)
        checklist.append(ChecklistItem(
            label="NULL-EIN dedup",
            expected="no duplicate",
            actual="no duplicate",
            passed=True,
        ))

    # ── Insert ────────────────────────────────────────────────────────────────
    doc = TaxDocument(**{
        k: v for k, v in doc_data.items()
        if k in {
            "tax_year", "form_type", "entity", "payer_name", "payer_ein",
            "recipient_name", "recipient_tin_last4", "amounts", "total_amount",
            "source_file", "notes", "status",
        }
    })
    db.add(doc)
    try:
        db.flush()  # obtain doc.id without committing
        doc_id = doc.id
        db.commit()
    except IntegrityError:
        db.rollback()
        checklist.append(ChecklistItem(
            label="duplicate check",
            expected="no duplicate",
            actual="duplicate document exists",
            passed=False,
        ))
        return IngestResult(
            success=False,
            document_id="",
            checklist=checklist,
            error="Duplicate: a document with the same tax year, form type, entity, and payer already exists",
        )

    # ── Read back ─────────────────────────────────────────────────────────────
    stored = db.get(TaxDocument, doc_id)
    if stored is None:
        return IngestResult(
            success=False,
            document_id=doc_id,
            checklist=checklist,
            error=f"Read-back failed: no row found for id={doc_id}",
        )

    # ── Build checklist ───────────────────────────────────────────────────────
    def _check(label: str, expected: Any, actual: Any) -> ChecklistItem:
        exp_str = str(expected)
        act_str = str(actual)
        # For Decimal/numeric comparison use Decimal equality
        if isinstance(expected, (int, float, Decimal)) or isinstance(actual, Decimal):
            try:
                passed = Decimal(str(expected)) == Decimal(str(actual))
            except Exception:
                passed = exp_str == act_str
        elif isinstance(expected, dict):
            passed = expected == actual
        else:
            passed = exp_str == act_str
        return ChecklistItem(label=label, expected=exp_str, actual=act_str, passed=passed)

    checklist.append(_check("form_type", doc_data.get("form_type"), stored.form_type))
    checklist.append(_check("payer_name", doc_data.get("payer_name"), stored.payer_name))
    checklist.append(_check("payer_ein", doc_data.get("payer_ein"), stored.payer_ein))
    checklist.append(_check("total_amount", doc_data.get("total_amount"), stored.total_amount))
    checklist.append(_check("amounts", doc_data.get("amounts", {}), stored.amounts))
    checklist.append(_check("entity", doc_data.get("entity"), stored.entity))
    checklist.append(_check("tax_year", doc_data.get("tax_year"), stored.tax_year))

    all_passed = all(item.passed for item in checklist)
    return IngestResult(success=all_passed, document_id=doc_id, checklist=checklist)


# ---------------------------------------------------------------------------
# list_documents
# ---------------------------------------------------------------------------

def list_documents(
    db: Session,
    year: int,
    entity: str | None = None,
) -> list[TaxDocument]:
    """List active tax documents for the given year, optionally filtered by entity.

    Returns documents ordered by form_type, payer_name.
    """
    q = (
        db.query(TaxDocument)
        .filter(
            TaxDocument.tax_year == year,
            TaxDocument.status == "active",
        )
    )
    if entity is not None:
        q = q.filter(TaxDocument.entity == entity)

    return q.order_by(TaxDocument.form_type, TaxDocument.payer_name).all()


# ---------------------------------------------------------------------------
# get_summary
# ---------------------------------------------------------------------------

def get_summary(db: Session, year: int) -> str:
    """Generate the filing-ready summary report for the given tax year.

    Groups by entity, includes IRS line mappings. Delegates formatting to
    generate_tax_doc_summary() in src/export/tax_doc_report.py.
    """
    docs = list_documents(db, year)
    flags = reconcile_light(db, year)

    doc_dicts = [_doc_to_dict(d) for d in docs]

    # Only include flags that actually have a difference flagged
    flag_dicts = [f for f in flags if f.get("flagged")]

    return generate_tax_doc_summary(doc_dicts, reconciliation_flags=flag_dicts if flag_dicts else None)


# ---------------------------------------------------------------------------
# reconcile_light
# ---------------------------------------------------------------------------

def reconcile_light(db: Session, year: int) -> list[dict[str, Any]]:
    """Compare tax document totals against the transaction register.

    For each active tax document in year:
    1. Try matching transactions on Transaction.payer_1099 (exact match,
       case-insensitive) filtered by entity + date range.
    2. Fall back to case-insensitive LIKE match on Transaction.description
       vs. doc.payer_name.
    3. Sum matched transaction amounts (using abs() due to sign convention).
    4. Flag when abs(tax_doc_total - transaction_sum) > 1.00.

    Returns list of dicts with reconciliation details for every document.
    Flagged documents have flagged=True.
    """
    docs = list_documents(db, year)
    results: list[dict[str, Any]] = []

    date_start = f"{year}-01-01"
    date_end = f"{year}-12-31"

    for doc in docs:
        # ── Pass 1: match on payer_1099 exact (case-insensitive) ─────────────
        matched_txs = (
            db.query(Transaction)
            .filter(
                func.lower(Transaction.payer_1099) == doc.payer_name.lower(),
                Transaction.entity == doc.entity,
                Transaction.date >= date_start,
                Transaction.date <= date_end,
            )
            .all()
        )

        # ── Pass 2: fallback — LIKE on description ────────────────────────────
        if not matched_txs:
            matched_txs = (
                db.query(Transaction)
                .filter(
                    func.lower(Transaction.description).contains(doc.payer_name.lower()),
                    Transaction.entity == doc.entity,
                    Transaction.date >= date_start,
                    Transaction.date <= date_end,
                )
                .all()
            )

        # Sum matched transaction amounts; use abs() to handle sign convention
        transaction_sum = Decimal("0")
        for tx in matched_txs:
            if tx.amount is not None:
                transaction_sum += abs(Decimal(str(tx.amount)))

        reported_amount = Decimal(str(doc.total_amount)) if doc.total_amount is not None else Decimal("0")
        difference = abs(reported_amount - transaction_sum)
        flagged = difference > Decimal("1.00")

        results.append({
            "doc_id": doc.id,
            "form_type": doc.form_type,
            "payer_name": doc.payer_name,
            "entity": doc.entity,
            "tax_year": doc.tax_year,
            "reported_amount": reported_amount,
            "transaction_sum": transaction_sum,
            "difference": difference,
            "matched_transaction_count": len(matched_txs),
            "flagged": flagged,
        })

        if flagged:
            logger.warning(
                "Reconciliation flag: %s %s — reported=%s, in register=%s, diff=%s",
                doc.form_type,
                doc.payer_name,
                reported_amount,
                transaction_sum,
                difference,
            )

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _doc_to_dict(doc: TaxDocument) -> dict[str, Any]:
    """Convert a TaxDocument ORM instance to a plain dict for report functions."""
    return {
        "id": doc.id,
        "tax_year": doc.tax_year,
        "form_type": doc.form_type,
        "entity": doc.entity,
        "payer_name": doc.payer_name,
        "payer_ein": doc.payer_ein,
        "recipient_name": doc.recipient_name,
        "recipient_tin_last4": doc.recipient_tin_last4,
        "amounts": doc.amounts,
        "total_amount": doc.total_amount,
        "source_file": doc.source_file,
        "notes": doc.notes,
        "status": doc.status,
        "created_at": str(doc.created_at),
        "updated_at": str(doc.updated_at),
    }
