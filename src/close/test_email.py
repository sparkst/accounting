"""Tests for close-email render + Resend send + ledger (REQ-MCA-001, spec §1.4)."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.close.email as email_mod
import src.db.connection  # noqa: F401  (register every ORM table on Base)
from src.alerts.models import AlertDispatch
from src.close.anomalies import AnomalyReport, NewVendor
from src.close.email import render_html, send_close_report
from src.close.reconcile import AccountRecon, ItemRecon, NeedsReviewBacklog, ReconcileSummary
from src.close.report import AutoConfirmSummary, CloseReport
from src.models.base import Base

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)
_TODAY = date(2026, 7, 7)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.rollback()
    s.query(AlertDispatch).delete()
    s.commit()
    s.close()


def _report() -> CloseReport:
    rec = ReconcileSummary(month="2026-06")
    rec.items.append(
        ItemRecon(
            item_id="i1",
            institution_name="Chase",
            status="active",
            covered_days=29,
            gap_days=["2026-06-30"],
            accounts=[
                AccountRecon(
                    payment_method="Chase ****1",
                    account_name="Checking",
                    plaid_account_type="depository",
                    is_depository=True,
                    register_count=3,
                    register_sum=Decimal("100.00"),
                    balance_delta=Decimal("100.00"),
                    tie_out_ok=True,
                    tie_out_gap=Decimal("0.00"),
                )
            ],
        )
    )
    rec.needs_review_backlog.append(NeedsReviewBacklog(entity="sparkry", count=4, oldest_date="2026-06-02"))
    an = AnomalyReport(month="2026-06")
    an.new_vendors.append(NewVendor(vendor_key="newco", entity="sparkry", count=2, total=Decimal("140.00")))
    ac = AutoConfirmSummary(total=12)
    return CloseReport(
        month="2026-06",
        generated_at="2026-07-07T15:00:00+00:00",
        rows_ingested=42,
        needs_review_depth=4,
        reconcile=rec,
        anomalies=an,
        autoconfirm=ac,
    )


def test_render_html_contains_sections_and_links() -> None:
    """REQ-MCA-001: the email renders every section with evidence links + hygiene."""
    html = render_html(_report())
    assert "Monthly Close — 2026-06" in html
    assert "Rows ingested" in html
    assert "Reconciliation" in html
    assert "Chase" in html
    assert "Anomalies" in html
    assert "newco" in html
    assert "books.sparkry.ai/transactions?vendor=newco" in html
    assert "books.sparkry.ai/?status=needs_review&amp;entity=sparkry" in html
    assert "Auto-confirm summary" in html
    assert "12 transaction(s) auto-confirmed" in html
    assert "Unnamed Vanguard taxable account — name or archive" in html
    assert "$50 Fidelity TOD — human closure decision" in html


def test_render_html_includes_narrative_when_present() -> None:
    """REQ-MCA-004: a narrative string is embedded when supplied."""
    html = render_html(_report(), narrative="A quiet month with no discrepancies.")
    assert "A quiet month with no discrepancies." in html


def test_dry_run_sends_nothing_and_writes_no_ledger(session: Session) -> None:
    """REQ-MCA-001: DRY-RUN default neither sends nor writes an AlertDispatch row."""
    result = send_close_report(session, _report(), apply=False)
    assert result is None
    assert session.query(AlertDispatch).count() == 0


def test_apply_sends_and_records_ledger(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-MCA-001: apply sends via Resend and records the monthly_close ledger row."""
    sent: dict[str, object] = {}

    class _FakeEmails:
        @staticmethod
        def send(params: dict[str, object]) -> dict[str, str]:
            sent.update(params)
            return {"id": "msg_1"}

    class _FakeResend:
        api_key = ""
        Emails = _FakeEmails

    monkeypatch.setitem(__import__("sys").modules, "resend", _FakeResend)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("ALERT_TO_EMAIL", "travis@sparkry.ai")

    row = send_close_report(session, _report(), apply=True, today=_TODAY)
    assert row is not None
    assert row.status == "sent"
    assert row.alert_type == "monthly_close"
    assert row.alert_key == "close:2026-06"
    assert row.delivery_channel == "resend_email"
    assert row.payload_json is None
    assert sent["subject"] == "Monthly close — 2026-06"
    assert session.query(AlertDispatch).count() == 1


def test_apply_records_failed_on_send_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-MCA-001: a send error is recorded as status=failed, never raised."""
    monkeypatch.setenv("ALERT_TO_EMAIL", "travis@sparkry.ai")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)  # forces the guard to raise
    row = send_close_report(session, _report(), apply=True, today=_TODAY)
    assert row is not None
    assert row.status == "failed"
    assert row.error_detail is not None


def test_module_constants() -> None:
    """REQ-MCA-001: ledger channel/type constants are stable."""
    assert email_mod.ALERT_TYPE == "monthly_close"
    assert email_mod.DELIVERY_CHANNEL == "resend_email"
