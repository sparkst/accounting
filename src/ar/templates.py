"""Template-based AR reminder drafts — three escalating tones (REQ-ARC-001).

No LLM. ``build_draft`` is a pure function returning ``(subject, body)`` for a
given invoice/customer/line-item context and ladder rung. The tone escalates:

    14 → friendly nudge
    30 → firm reminder
    45 → final notice (references ``late_fee_pct`` when set > 0)

Amounts are formatted through ``_format_currency`` from the invoicing email
sender so currency rendering is identical across the two send paths.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.invoicing.email_sender import _format_currency

# Rung → short human label for subjects/logs.
RUNG_LABELS = {14: "friendly nudge", 30: "firm reminder", 45: "final notice"}


def _customer_name(customer: Any) -> str:
    """Prefer the named contact, then the company name, then a neutral greeting."""
    contact = getattr(customer, "contact_name", None)
    if contact:
        return str(contact)
    name = getattr(customer, "name", None)
    if name:
        return str(name)
    return "there"


def _format_date(value: str | date | None) -> str:
    if value is None:
        return "the invoice due date"
    if isinstance(value, date):
        return value.strftime("%m/%d/%Y")
    try:
        return date.fromisoformat(str(value)).strftime("%m/%d/%Y")
    except ValueError:
        return str(value)


def _line_item_lines(line_items: list[Any]) -> list[str]:
    lines: list[str] = []
    for item in line_items:
        desc = getattr(item, "description", "")
        total = getattr(item, "total_price", 0)
        lines.append(f"  - {desc}: {_format_currency(total)}")
    return lines


def _late_fee_pct(invoice: Any) -> float:
    raw = getattr(invoice, "late_fee_pct", 0.0)
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def build_draft(
    invoice: Any,
    customer: Any,
    line_items: list[Any],
    rung: int,
) -> tuple[str, str]:
    """Render ``(subject, body)`` for one reminder rung. Pure — no I/O.

    REQ-ARC-001: the body escalates in tone by rung; the 45-day final notice
    references the late-fee percentage only when the invoice carries one > 0.
    """
    number = getattr(invoice, "invoice_number", getattr(invoice, "id", "?"))
    amount = _format_currency(getattr(invoice, "total", 0))
    due = _format_date(getattr(invoice, "due_date", None))
    name = _customer_name(customer)
    items = _line_item_lines(line_items)

    if rung == 14:
        subject = f"Friendly reminder — invoice {number}"
        opening = (
            f"Hi {name},\n\n"
            f"I hope you're doing well. This is a friendly reminder that "
            f"invoice {number} for {amount}, due {due}, is now outstanding. "
            f"If it's already on its way, please disregard this note — no rush."
        )
        closing = "Thanks so much for your business."
    elif rung == 30:
        subject = f"Second reminder — invoice {number} is past due"
        opening = (
            f"Hi {name},\n\n"
            f"Our records show invoice {number} for {amount}, due {due}, is now "
            f"past due. Could you please arrange payment at your earliest "
            f"convenience, or let me know if there's anything holding it up?"
        )
        closing = "I appreciate your prompt attention to this."
    elif rung == 45:
        subject = f"Final notice — invoice {number} is significantly overdue"
        opening = (
            f"Hi {name},\n\n"
            f"This is a final notice regarding invoice {number} for {amount}, "
            f"which was due {due} and remains unpaid. Please remit payment "
            f"promptly so we can keep your account in good standing."
        )
        pct = _late_fee_pct(invoice)
        if pct > 0:
            opening += (
                f"\n\nPer the agreed terms, a late fee of {pct * 100:g}% may now "
                f"be applied to the outstanding balance."
            )
        closing = "Please reach out right away if you need to discuss payment."
    else:  # pragma: no cover - guarded by AR_RUNGS callers
        raise ValueError(f"unknown reminder rung: {rung}")

    body_parts = [opening, ""]
    if items:
        body_parts.append("Invoice detail:")
        body_parts.extend(items)
        body_parts.append("")
    body_parts.append(f"Amount due: {amount}")
    body_parts.append("")
    body_parts.append(closing)
    body_parts.append("")
    body_parts.append("Sparkry LLC")

    return subject, "\n".join(body_parts)
