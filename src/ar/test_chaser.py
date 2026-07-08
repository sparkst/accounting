"""Tests for the AR chaser ladder, dismissal, and aging buckets (REQ-ARC-001/003)."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import src.ar.chaser as chaser
import src.ar.notify as notify_mod
from src.alerts.models import AlertDispatch
from src.alerts.webhook import WebhookResult
from src.ar.chaser import aging_buckets, run
from src.models.ar_reminder import (
    AR_STATUS_DISMISSED,
    AR_STATUS_DRAFTED,
    AR_STATUS_PENDING_APPROVAL,
    AR_STATUS_SENT,
    ArReminder,
)
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.enums import InvoiceStatus
from src.models.invoice import Customer, Invoice, InvoiceLineItem

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
# Create only the tables these tests touch — the full Base.metadata carries
# unrelated FK targets (brokerage/plaid) that may not be imported yet at
# collection time, which would break a blanket create_all().
_TABLES: list[Any] = [
    Customer.__table__,
    Invoice.__table__,
    InvoiceLineItem.__table__,
    ArReminder.__table__,
    AuditEvent.__table__,
    AlertDispatch.__table__,
]
Base.metadata.create_all(_ENGINE, tables=_TABLES)
_Session = sessionmaker(bind=_ENGINE)

TODAY = date(2026, 7, 1)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    for model in (AuditEvent, ArReminder, InvoiceLineItem, Invoice, Customer, AlertDispatch):
        s.query(model).delete()
    s.commit()
    s.close()


@pytest.fixture(autouse=True)
def _stub_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never hit the network in ladder tests; count via the fake's return."""
    monkeypatch.setattr(
        notify_mod,
        "notify_draft",
        lambda *a, **k: WebhookResult("sent", 200, None),
    )


_seq = 0


def _make_invoice(
    session: Session,
    *,
    days_since_sent: int | None,
    status: str = InvoiceStatus.SENT.value,
    paid_date: str | None = None,
    total: str = "1000.00",
    late_fee_pct: float = 0.0,
) -> Invoice:
    global _seq
    _seq += 1
    customer = Customer(
        name=f"Cust {_seq}",
        contact_name="Jane",
        contact_email="jane@example.com",
        billing_model="hourly",
    )
    session.add(customer)
    session.flush()
    sent_at = (
        datetime.combine(TODAY - timedelta(days=days_since_sent), datetime.min.time())
        if days_since_sent is not None
        else None
    )
    invoice = Invoice(
        invoice_number=f"INV-{_seq}",
        customer_id=customer.id,
        entity="sparkry",
        status=status,
        subtotal=Decimal(total),
        total=Decimal(total),
        sent_at=sent_at,
        paid_date=paid_date,
        due_date="2026-06-01",
        late_fee_pct=late_fee_pct,
    )
    session.add(invoice)
    session.flush()
    session.add(
        InvoiceLineItem(
            invoice_id=invoice.id,
            description="Work",
            quantity=Decimal("1"),
            unit_price=Decimal(total),
            total_price=Decimal(total),
        )
    )
    session.commit()
    return invoice


def _rungs(session: Session, invoice_id: str) -> list[int]:
    return sorted(
        r.rung
        for r in session.query(ArReminder)
        .filter(ArReminder.invoice_id == invoice_id)
        .all()
    )


def test_no_draft_before_first_rung_day_13(session: Session) -> None:
    """REQ-ARC-001: nothing drafts before day 14."""
    inv = _make_invoice(session, days_since_sent=13)
    summary = run(session, today=TODAY, apply=True)
    assert summary.drafted == 0
    assert _rungs(session, inv.id) == []


def test_drafts_rung_14_at_day_14(session: Session) -> None:
    """REQ-ARC-001: the 14-day rung drafts exactly at day 14."""
    inv = _make_invoice(session, days_since_sent=14)
    summary = run(session, today=TODAY, apply=True)
    assert summary.drafted == 1
    assert _rungs(session, inv.id) == [14]


def test_highest_rung_only_at_day_44(session: Session) -> None:
    """REQ-ARC-001: at day 44 only the highest due rung (30) drafts."""
    inv = _make_invoice(session, days_since_sent=44)
    run(session, today=TODAY, apply=True)
    assert _rungs(session, inv.id) == [30]


def test_highest_rung_only_at_day_46(session: Session) -> None:
    """REQ-ARC-001: an invoice discovered at day 46 gets one 45-day draft, not three."""
    inv = _make_invoice(session, days_since_sent=46)
    summary = run(session, today=TODAY, apply=True)
    assert summary.drafted == 1
    assert _rungs(session, inv.id) == [45]


def test_one_rung_per_run_idempotent(session: Session) -> None:
    """REQ-ARC-001: re-running the same day never duplicates a rung."""
    inv = _make_invoice(session, days_since_sent=20)
    run(session, today=TODAY, apply=True)
    run(session, today=TODAY, apply=True)  # second pass same day
    assert _rungs(session, inv.id) == [14]


def test_dry_run_writes_nothing(session: Session) -> None:
    """REQ-ARC-001: DRY-RUN computes counts but writes no rows and no webhook."""
    inv = _make_invoice(session, days_since_sent=46)
    summary = run(session, today=TODAY, apply=False)
    assert summary.drafted == 1
    assert session.query(ArReminder).count() == 0
    assert _rungs(session, inv.id) == []


def test_draft_transitions_to_pending_and_audits(session: Session) -> None:
    """REQ-ARC-003: an applied draft moves drafted→pending_approval + audit row."""
    inv = _make_invoice(session, days_since_sent=14)
    run(session, today=TODAY, apply=True)
    reminder = (
        session.query(ArReminder).filter(ArReminder.invoice_id == inv.id).one()
    )
    assert reminder.status == AR_STATUS_PENDING_APPROVAL
    audits = (
        session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "ar_reminder",
            AuditEvent.entity_id == reminder.id,
            AuditEvent.field_changed == "status",
        )
        .all()
    )
    assert any(
        a.old_value == AR_STATUS_DRAFTED
        and a.new_value == AR_STATUS_PENDING_APPROVAL
        for a in audits
    )


def test_paid_invoice_dismisses_open_drafts(session: Session) -> None:
    """REQ-ARC-001: a paid invoice dismisses its open drafts (+ audit)."""
    inv = _make_invoice(session, days_since_sent=20)
    run(session, today=TODAY, apply=True)
    reminder = (
        session.query(ArReminder).filter(ArReminder.invoice_id == inv.id).one()
    )
    # Invoice gets paid.
    inv.status = InvoiceStatus.PAID.value
    inv.paid_date = "2026-07-01"
    session.commit()

    run(session, today=TODAY, apply=True)
    session.refresh(reminder)
    assert reminder.status == AR_STATUS_DISMISSED
    dismiss_audit = (
        session.query(AuditEvent)
        .filter(
            AuditEvent.entity_id == reminder.id,
            AuditEvent.new_value == AR_STATUS_DISMISSED,
        )
        .count()
    )
    assert dismiss_audit == 1


def test_void_invoice_dismisses_open_drafts(session: Session) -> None:
    """REQ-ARC-001: a void invoice also dismisses open drafts."""
    inv = _make_invoice(session, days_since_sent=20)
    run(session, today=TODAY, apply=True)
    inv.status = InvoiceStatus.VOID.value
    session.commit()
    run(session, today=TODAY, apply=True)
    reminder = (
        session.query(ArReminder).filter(ArReminder.invoice_id == inv.id).one()
    )
    assert reminder.status == AR_STATUS_DISMISSED


def test_invoice_without_sent_at_skipped(session: Session) -> None:
    """REQ-ARC-001: an invoice with no sent_at is never chased."""
    inv = _make_invoice(session, days_since_sent=None)
    summary = run(session, today=TODAY, apply=True)
    assert summary.drafted == 0
    assert _rungs(session, inv.id) == []


def test_dismiss_reminder_cannot_clobber_a_concurrently_sent_row(
    session: Session,
) -> None:
    """P3-001-2: a racing dismiss must not overwrite an already-sent status.

    Simulates the race: the caller holds a stale in-memory ``reminder``
    (status='pending_approval') while a concurrent ``approve_and_send`` — in
    its own session — has already claimed and sent it, flipping the DB row to
    'sent'. The guarded UPDATE...WHERE in dismiss_reminder must see the
    current DB state, find no matching row, and refuse the transition rather
    than blindly writing 'dismissed' over 'sent'.
    """
    inv = _make_invoice(session, days_since_sent=20)
    reminder = ArReminder(
        invoice_id=inv.id,
        rung=14,
        status=AR_STATUS_PENDING_APPROVAL,
        draft_subject="s",
        draft_body="b",
    )
    session.add(reminder)
    session.commit()

    # Load the object's attributes in THIS session post-commit so the identity
    # map holds them, then flip the DB row through an INDEPENDENT session
    # (P1-202): a same-session .update() (even with synchronize_session=False,
    # because commit() expires the identity map) would refresh `reminder`
    # in-memory and the trivial early-return would satisfy this test without
    # ever exercising the guarded UPDATE...WHERE claim.
    assert reminder.status == AR_STATUS_PENDING_APPROVAL  # loads post-commit
    other = _Session()
    try:
        other.query(ArReminder).filter_by(id=reminder.id).update(
            {"status": "sent"}, synchronize_session=False
        )
        other.commit()
    finally:
        other.close()
    # The caller's in-memory view is genuinely stale now.
    assert reminder.status == AR_STATUS_PENDING_APPROVAL

    changed = chaser.dismiss_reminder(session, reminder, changed_by="cli")

    assert changed is False
    persisted = session.get(ArReminder, reminder.id)
    assert persisted is not None
    assert persisted.status == "sent"  # never clobbered to 'dismissed'


def test_unique_rung_violation_skips_not_crashes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-001: a racing duplicate-rung insert is skipped, not fatal."""
    inv = _make_invoice(session, days_since_sent=20)
    # Pre-seed the 14-rung row, then force the ladder to re-pick 14 anyway,
    # exercising the UNIQUE(invoice_id, rung) begin_nested skip path.
    session.add(
        ArReminder(
            invoice_id=inv.id,
            rung=14,
            status=AR_STATUS_DRAFTED,
            draft_subject="s",
            draft_body="b",
        )
    )
    session.commit()
    monkeypatch.setattr(chaser, "_highest_unsent_rung", lambda days, existing: 14)

    summary = run(session, today=TODAY, apply=True)  # must not raise
    assert summary.drafted == 0
    assert _rungs(session, inv.id) == [14]  # still exactly one


def test_model_enforces_unique_invoice_rung(session: Session) -> None:
    """REQ-ARC-001: the DB constraint guarantees exactly-once per (invoice, rung)."""
    inv = _make_invoice(session, days_since_sent=20)
    for _ in range(2):
        session.add(
            ArReminder(
                invoice_id=inv.id,
                rung=30,
                status=AR_STATUS_DRAFTED,
                draft_subject="s",
                draft_body="b",
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_run_apply_sweeps_failed_ar_reminder_notify_rows(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-002: a failed ar_reminder_notify row is replayed on the next --apply run.

    Reproduces the orphan: a prior invocation's n8n POST failed (row persisted
    with status='failed', delivery_channel='n8n_webhook', payload_json set).
    The next daily ``run --apply`` must retry it via the shared REQ-FIX-ALR-002
    sweep, not leave it stuck forever with no automated retry.
    """
    failed_row = AlertDispatch(
        alert_key="ar:some-invoice:14",
        occurrence_date=TODAY.isoformat(),
        alert_type="ar_reminder_notify",
        entity="sparkry",
        subject="AR reminder draft",
        status="failed",
        http_status=None,
        error_detail="connection reset",
        delivery_channel="n8n_webhook",
        payload_json='{"alert_key": "ar:some-invoice:14", "type": "info"}',
    )
    session.add(failed_row)
    session.commit()

    calls: list[dict[str, object]] = []

    def _fake_sweep_post(payload: dict[str, object]) -> WebhookResult:
        calls.append(payload)
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(chaser, "_sweep_post", _fake_sweep_post)

    # No due invoices this run — isolates the assertion to the sweep step.
    run(session, today=TODAY, apply=True)

    assert len(calls) == 1
    assert calls[0]["alert_key"] == "ar:some-invoice:14"
    session.refresh(failed_row)
    assert failed_row.status == "sent"


def test_run_apply_neutralizes_stale_notify_row_for_terminal_reminder(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-205: a failed ar_reminder_notify row for an already-terminal (sent)
    reminder must be neutralized, not re-POSTed — nobody can act on a stale
    approval card for a reminder that's already been sent."""
    inv = _make_invoice(session, days_since_sent=20)
    reminder = ArReminder(
        invoice_id=inv.id,
        rung=14,
        status=AR_STATUS_SENT,
        draft_subject="s",
        draft_body="b",
    )
    session.add(reminder)
    session.commit()

    failed_row = AlertDispatch(
        alert_key=f"ar:{inv.id}:14",
        occurrence_date=TODAY.isoformat(),
        alert_type="ar_reminder_notify",
        entity="sparkry",
        subject="AR reminder draft",
        status="failed",
        http_status=None,
        error_detail="connection reset",
        delivery_channel="n8n_webhook",
        payload_json=f'{{"alert_key": "ar:{inv.id}:14", "type": "info"}}',
    )
    session.add(failed_row)
    session.commit()

    calls: list[dict[str, object]] = []

    def _fake_sweep_post(payload: dict[str, object]) -> WebhookResult:
        calls.append(payload)
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(chaser, "_sweep_post", _fake_sweep_post)

    run(session, today=TODAY, apply=True)

    assert calls == []
    session.refresh(failed_row)
    assert failed_row.status == "skipped"
    assert failed_row.error_detail == "superseded: reminder terminal"


def test_run_apply_still_sweeps_stale_notify_row_for_pending_reminder(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-205: a failed ar_reminder_notify row for a still-actionable (pending)
    reminder is unaffected by the neutralize pass and is still swept."""
    inv = _make_invoice(session, days_since_sent=20)
    reminder = ArReminder(
        invoice_id=inv.id,
        rung=14,
        status=AR_STATUS_PENDING_APPROVAL,
        draft_subject="s",
        draft_body="b",
    )
    session.add(reminder)
    session.commit()

    failed_row = AlertDispatch(
        alert_key=f"ar:{inv.id}:14",
        occurrence_date=TODAY.isoformat(),
        alert_type="ar_reminder_notify",
        entity="sparkry",
        subject="AR reminder draft",
        status="failed",
        http_status=None,
        error_detail="connection reset",
        delivery_channel="n8n_webhook",
        payload_json=f'{{"alert_key": "ar:{inv.id}:14", "type": "info"}}',
    )
    session.add(failed_row)
    session.commit()

    calls: list[dict[str, object]] = []

    def _fake_sweep_post(payload: dict[str, object]) -> WebhookResult:
        calls.append(payload)
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(chaser, "_sweep_post", _fake_sweep_post)

    run(session, today=TODAY, apply=True)

    assert len(calls) == 1
    assert calls[0]["alert_key"] == f"ar:{inv.id}:14"
    session.refresh(failed_row)
    assert failed_row.status == "sent"


def test_aging_buckets_math(session: Session) -> None:
    """REQ-ARC-003: aging buckets sum abs(total) by days past sent_at."""
    _make_invoice(session, days_since_sent=5, total="100.00")  # current
    _make_invoice(session, days_since_sent=20, total="200.00")  # 14 bucket
    _make_invoice(session, days_since_sent=35, total="400.00")  # 30 bucket
    _make_invoice(session, days_since_sent=60, total="800.00")  # 45+ bucket
    # Paid invoice excluded.
    _make_invoice(
        session,
        days_since_sent=60,
        total="9999.00",
        status=InvoiceStatus.PAID.value,
        paid_date="2026-06-01",
    )
    buckets = aging_buckets(session, as_of=TODAY)
    assert buckets["current"] == Decimal("100.00")
    assert buckets["14"] == Decimal("200.00")
    assert buckets["30"] == Decimal("400.00")
    assert buckets["45+"] == Decimal("800.00")
