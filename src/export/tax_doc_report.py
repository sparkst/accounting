"""Tax document filing-ready summary report.

Pure function — no DB access, no I/O side effects.
Accepts lists of tax document dicts and produces formatted plain-text output.

Design spec: §Tax Summary Report
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# ---------------------------------------------------------------------------
# Entity display names
# ---------------------------------------------------------------------------

ENTITY_DISPLAY: dict[str, str] = {
    "personal": "Personal",
    "sparkry": "Sparkry AI LLC",
    "blackline": "BlackLine MTB LLC",
}

# Entity sort order (matches design spec grouping)
ENTITY_ORDER = ["personal", "sparkry", "blackline"]

# ---------------------------------------------------------------------------
# IRS line mapping
# ---------------------------------------------------------------------------

IRS_LINE_MAP: dict[str, str] = {
    "1099-NEC": "Schedule C / Line 1",
    "1099-INT": "Schedule B / Line 1",
    "1099-DIV": "Schedule B / Line 5",
    "1099-B": "Schedule D / Form 8949",
    "1099-K": "Schedule C / Line 1",
    "K-1": "Schedule E / Part II",
    "1098": "Schedule A / Line 8a",
    "PROPERTY_TAX": "Schedule A / SALT",
    "OTHER": "See notes",
}

# Form types whose total_amount is not shown as a dollar figure (show placeholder)
_PLACEHOLDER_AMOUNT_FORMS = {"1099-B"}

# Short form type labels for the report
FORM_LABELS: dict[str, str] = {
    "1099-NEC": "1099-NEC",
    "1099-INT": "1099-INT",
    "1099-DIV": "1099-DIV",
    "1099-B": "1099-B",
    "1099-K": "1099-K",
    "K-1": "K-1",
    "1098": "1098",
    "PROPERTY_TAX": "Prop Tax",
    "OTHER": "Other",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_decimal(value: Any) -> Decimal:
    """Safely convert a value to Decimal, returning Decimal(0) on failure."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _fmt_currency(amount: Any) -> str:
    """Format a numeric value as a dollar string: $1,234.56"""
    d = _to_decimal(amount)
    return f"${d:,.2f}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_tax_doc_summary(
    documents: list[dict[str, Any]],
    reconciliation_flags: list[dict[str, Any]] | None = None,
) -> str:
    """Generate a filing-ready tax document summary report.

    Args:
        documents: List of tax document dicts. Expected keys:
            form_type, payer_name, total_amount, entity, tax_year, status.
            Inactive (soft-deleted) documents are excluded automatically.
        reconciliation_flags: Optional list of flag dicts from reconcile_light().
            Each dict has: doc_id, form_type, payer_name, entity,
            reported_amount, transaction_sum, difference.

    Returns:
        Formatted plain-text report grouped by entity with IRS line mappings.
    """
    # Filter to active documents only
    active_docs = [d for d in documents if d.get("status", "active") == "active"]

    if not active_docs:
        return "No active tax documents found.\n"

    # Determine tax year from documents (use most common or first found)
    years = [d.get("tax_year") for d in active_docs if d.get("tax_year")]
    year = years[0] if years else "Unknown"

    # Group documents by entity
    by_entity: dict[str, list[dict[str, Any]]] = {}
    for doc in active_docs:
        entity = doc.get("entity", "personal")
        by_entity.setdefault(entity, []).append(doc)

    # Sort within each entity: by form_type, then payer_name
    for entity_docs in by_entity.values():
        entity_docs.sort(key=lambda d: (d.get("form_type", ""), d.get("payer_name", "")))

    # Build reconciliation flag lookup by doc_id
    flag_by_id: dict[str, dict[str, Any]] = {}
    if reconciliation_flags:
        for flag in reconciliation_flags:
            doc_id = flag.get("doc_id", "")
            if doc_id:
                flag_by_id[doc_id] = flag

    # ── Render report ──────────────────────────────────────────────────────────
    lines: list[str] = []

    # Render entities in canonical order, then any extras not in ENTITY_ORDER
    ordered_entities = [e for e in ENTITY_ORDER if e in by_entity]
    extra_entities = [e for e in by_entity if e not in ENTITY_ORDER]

    for entity in ordered_entities + extra_entities:
        entity_docs = by_entity[entity]
        display_name = ENTITY_DISPLAY.get(entity, entity.title())

        lines.append(f"{'═' * 60}")
        lines.append(f"  {year} Tax Documents — {display_name}")
        lines.append(f"{'═' * 60}")
        lines.append("")

        # Column header
        lines.append(
            f"{'Form':<12} {'Payer':<34} {'Amount':>12}  {'IRS Line'}"
        )
        lines.append(f"{'─' * 11} {'─' * 34} {'─' * 12}  {'─' * 24}")

        for doc in entity_docs:
            form_type = doc.get("form_type", "")
            form_label = FORM_LABELS.get(form_type, form_type)
            payer = doc.get("payer_name", "")[:34]  # truncate for column width
            irs_line = IRS_LINE_MAP.get(form_type, "See notes")

            if form_type in _PLACEHOLDER_AMOUNT_FORMS:
                amount_str = f"{'(see CSV)':>12}"
            else:
                amount_str = f"{_fmt_currency(doc.get('total_amount')):>12}"

            lines.append(f"{form_label:<12} {payer:<34} {amount_str}  {irs_line}")

        lines.append("")

    # ── Reconciliation warnings ────────────────────────────────────────────────
    if reconciliation_flags:
        lines.append(f"{'─' * 60}")
        lines.append("  Reconciliation Warnings")
        lines.append(f"{'─' * 60}")
        lines.append("")
        for flag in reconciliation_flags:
            form_type = flag.get("form_type", "")
            payer = flag.get("payer_name", "")
            reported = _fmt_currency(flag.get("reported_amount"))
            tx_sum = _fmt_currency(flag.get("transaction_sum"))
            diff = _fmt_currency(flag.get("difference"))
            lines.append(
                f"  WARNING {form_type} {payer}: "
                f"{reported} reported, {tx_sum} in register (diff: {diff})"
            )
        lines.append("")

    return "\n".join(lines)
