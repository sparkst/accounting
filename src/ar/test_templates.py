"""Tests for AR reminder draft templates (REQ-ARC-001)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.ar.templates import build_draft


def _invoice(late_fee_pct: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(
        id="inv-1",
        invoice_number="202606-007",
        total=Decimal("1500.00"),
        due_date="2026-05-14",
        late_fee_pct=late_fee_pct,
    )


def _customer(contact_name: str | None = "Jane Doe") -> SimpleNamespace:
    return SimpleNamespace(name="Acme Corp", contact_name=contact_name)


def _line_items() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(description="Coaching — May", total_price=Decimal("1500.00")),
    ]


def test_friendly_nudge_tone_rung_14() -> None:
    """REQ-ARC-001: 14-day draft uses a friendly, low-pressure tone."""
    subject, body = build_draft(_invoice(), _customer(), _line_items(), 14)
    assert "Friendly reminder" in subject
    assert "202606-007" in subject
    assert "Hi Jane Doe," in body
    assert "friendly reminder" in body.lower()
    assert "no rush" in body.lower()
    assert "$1,500.00" in body


def test_firm_reminder_tone_rung_30() -> None:
    """REQ-ARC-001: 30-day draft escalates to a firm past-due tone."""
    subject, body = build_draft(_invoice(), _customer(), _line_items(), 30)
    assert "past due" in subject.lower()
    assert "past due" in body.lower()
    assert "$1,500.00" in body


def test_final_notice_references_late_fee_when_set_rung_45() -> None:
    """REQ-ARC-001: 45-day final notice references late_fee_pct when > 0."""
    subject, body = build_draft(_invoice(late_fee_pct=0.10), _customer(), _line_items(), 45)
    assert "final notice" in subject.lower()
    assert "final notice" in body.lower()
    assert "10%" in body
    assert "late fee" in body.lower()


def test_final_notice_omits_late_fee_when_zero() -> None:
    """REQ-ARC-001: no late-fee line when the invoice carries no late fee."""
    _subject, body = build_draft(_invoice(late_fee_pct=0.0), _customer(), _line_items(), 45)
    assert "late fee" not in body.lower()


def test_missing_contact_name_uses_neutral_greeting() -> None:
    """REQ-ARC-001: a customer without a contact name still renders cleanly."""
    _subject, body = build_draft(_invoice(), _customer(contact_name=None), _line_items(), 14)
    # Falls back to company name.
    assert "Acme Corp" in body


def test_unknown_rung_rejected() -> None:
    """REQ-ARC-001: only ladder rungs render; a bad rung is a programming error."""
    with pytest.raises(ValueError):
        build_draft(_invoice(), _customer(), _line_items(), 99)
