"""Tests for alert rule generation.

REQ-ID: REQ-ALERT-001 (Sparkry monthly B&O escalation days)
REQ-ID: REQ-ALERT-002 (BlackLine quarterly B&O escalation)
REQ-ID: REQ-ALERT-003 (tax_bo body has account id, period, due date, login url)
"""

from __future__ import annotations

from datetime import date

from src.alerts.rules import Alert, compute_tax_alerts


def _by_key(alerts: list[Alert]) -> dict[str, Alert]:
    return {a.alert_key: a for a in alerts}


def test_sparkry_monthly_fires_on_reminder_days() -> None:
    # 2026-05-10 is a reminder day (10th); period is prior month = April 2026.
    alerts = compute_tax_alerts(date(2026, 5, 10))
    sparkry = _by_key(alerts).get("tax:sparkry:bo:2026-04")
    assert sparkry is not None
    assert sparkry.alert_type == "tax_bo"
    assert sparkry.entity == "sparkry"
    assert sparkry.due_date == "2026-05-25"
    assert sparkry.occurrence_date == "2026-05-10"
    # Body must carry the actionable context.
    assert "605-965-107" in sparkry.body_text  # DOR account
    assert "April 2026" in sparkry.body_text  # filing period
    assert sparkry.due_date is not None
    assert sparkry.due_date in sparkry.body_text  # due date rendered in body
    assert "https://secure.dor.wa.gov/home/Login" in sparkry.action_url


def test_sparkry_monthly_silent_on_non_reminder_day() -> None:
    # 2026-05-11 is not in {3,10,17,25}.
    alerts = compute_tax_alerts(date(2026, 5, 11))
    assert "tax:sparkry:bo:2026-04" not in _by_key(alerts)


def test_sparkry_fires_on_due_date_25th() -> None:
    alerts = compute_tax_alerts(date(2026, 5, 25))
    assert "tax:sparkry:bo:2026-04" in _by_key(alerts)


def test_blackline_q1_fires_in_april_reminder_window() -> None:
    # Q1 (Jan-Mar) is due Apr 30; April 17 is a reminder day.
    alerts = compute_tax_alerts(date(2026, 4, 17))
    bl = _by_key(alerts).get("tax:blackline:bo:2026-Q1")
    assert bl is not None
    assert bl.entity == "blackline"
    assert bl.due_date == "2026-04-30"
    assert "605-922-410" in bl.body_text
    assert "Q1" in bl.body_text
    assert bl.due_date is not None
    assert bl.due_date in bl.body_text  # due date rendered in body


def test_blackline_q1_fires_on_due_date_apr_30() -> None:
    alerts = compute_tax_alerts(date(2026, 4, 30))
    assert "tax:blackline:bo:2026-Q1" in _by_key(alerts)


def test_blackline_q4_uses_prior_year_in_january() -> None:
    # January 2026 due date is Jan 31 for Q4 of 2025.
    alerts = compute_tax_alerts(date(2026, 1, 10))
    bl = _by_key(alerts).get("tax:blackline:bo:2025-Q4")
    assert bl is not None
    assert bl.due_date == "2026-01-31"


def test_no_tax_alerts_on_quiet_month_day() -> None:
    # A non-due month (e.g. May) day that is not a Sparkry reminder day.
    alerts = compute_tax_alerts(date(2026, 5, 13))
    assert alerts == []


def test_sparkry_silent_after_due_date() -> None:
    # 2026-05-26 is after the 25th due date — no Sparkry April alert.
    alerts = compute_tax_alerts(date(2026, 5, 26))
    assert "tax:sparkry:bo:2026-04" not in _by_key(alerts)


def test_blackline_q2_and_q3() -> None:
    # July 10 → Q2 (Apr-Jun) due 2026-07-31.
    bl_q2 = _by_key(compute_tax_alerts(date(2026, 7, 10))).get("tax:blackline:bo:2026-Q2")
    assert bl_q2 is not None
    assert bl_q2.due_date == "2026-07-31"
    # October 17 → Q3 (Jul-Sep) due 2026-10-31.
    bl_q3 = _by_key(compute_tax_alerts(date(2026, 10, 17))).get("tax:blackline:bo:2026-Q3")
    assert bl_q3 is not None
    assert bl_q3.due_date == "2026-10-31"


def test_blackline_silent_after_due_date() -> None:
    # May is not a BlackLine due month — no blackline alert.
    alerts = compute_tax_alerts(date(2026, 5, 1))
    assert "tax:blackline:bo:2026-Q1" not in _by_key(alerts)


# --- Invoice rules ---------------------------------------------------------

from collections.abc import Generator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from src.alerts.rules import compute_invoice_alerts  # noqa: E402
from src.models.base import Base  # noqa: E402
from src.models.enums import BillingModel  # noqa: E402
from src.models.invoice import Customer, Invoice  # noqa: E402
from src.models.transaction import Transaction  # noqa: F401, E402  # registers FK table

_INV_ENGINE = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}
)
Base.metadata.create_all(_INV_ENGINE)
_InvSession = sessionmaker(bind=_INV_ENGINE)


@pytest.fixture()
def inv_session() -> Generator[Session, None, None]:
    s = _InvSession()
    yield s
    # Clean rows between tests for isolation.
    s.query(Invoice).delete()
    s.query(Customer).delete()
    s.commit()
    s.close()


def _make_customer(s: Session) -> str:
    # Customer has no `entity` column; billing_model is required.
    c = Customer(name="Cardinal Health", billing_model=BillingModel.FLAT_RATE.value)
    s.add(c)
    s.commit()
    return c.id


def _make_draft(s: Session, *, number: str, due: str | None, submitted: str | None) -> str:
    cid = _make_customer(s)
    inv = Invoice(
        invoice_number=number,
        customer_id=cid,
        entity="sparkry",
        status="draft",
        due_date=due,
        submitted_date=submitted,
        subtotal=0,
        total=100,
    )
    s.add(inv)
    s.commit()
    return inv.id


def test_sweep_fires_on_last_day_of_month(inv_session: Session) -> None:
    alerts = compute_invoice_alerts(date(2026, 6, 30), inv_session)
    keys = {a.alert_key for a in alerts}
    assert "invoice:sweep:2026-06" in keys


def test_sweep_subject_matches_spec(inv_session: Session) -> None:
    alerts = compute_invoice_alerts(date(2026, 6, 30), inv_session)
    sweep = next(a for a in alerts if a.alert_key == "invoice:sweep:2026-06")
    assert sweep.subject == "Time to create & submit June 2026 invoices"


def test_sweep_silent_on_non_last_day(inv_session: Session) -> None:
    alerts = compute_invoice_alerts(date(2026, 6, 29), inv_session)
    keys = {a.alert_key for a in alerts}
    assert "invoice:sweep:2026-06" not in keys


def test_sweep_handles_february(inv_session: Session) -> None:
    alerts = compute_invoice_alerts(date(2026, 2, 28), inv_session)
    assert "invoice:sweep:2026-02" in {a.alert_key for a in alerts}


def test_draft_invoice_fires_daily_from_due_date(inv_session: Session) -> None:
    inv_id = _make_draft(inv_session, number="202606-001", due="2026-06-05", submitted=None)
    # On due date.
    a1 = compute_invoice_alerts(date(2026, 6, 5), inv_session)
    # A few days later — still draft, still fires.
    a2 = compute_invoice_alerts(date(2026, 6, 9), inv_session)
    assert f"invoice:draft:{inv_id}" in {a.alert_key for a in a1}
    assert f"invoice:draft:{inv_id}" in {a.alert_key for a in a2}


def test_draft_invoice_silent_before_due_date(inv_session: Session) -> None:
    inv_id = _make_draft(inv_session, number="202606-002", due="2026-06-20", submitted=None)
    alerts = compute_invoice_alerts(date(2026, 6, 1), inv_session)
    assert f"invoice:draft:{inv_id}" not in {a.alert_key for a in alerts}


def test_non_draft_invoice_never_fires(inv_session: Session) -> None:
    cid = _make_customer(inv_session)
    inv = Invoice(
        invoice_number="202606-003",
        customer_id=cid,
        entity="sparkry",
        status="sent",
        due_date="2026-06-01",
        subtotal=0,
        total=100,
    )
    inv_session.add(inv)
    inv_session.commit()
    alerts = compute_invoice_alerts(date(2026, 6, 10), inv_session)
    assert f"invoice:draft:{inv.id}" not in {a.alert_key for a in alerts}


def test_draft_falls_back_to_submitted_date_when_due_missing(inv_session: Session) -> None:
    inv_id = _make_draft(inv_session, number="202606-004", due=None, submitted="2026-06-03")
    alerts = compute_invoice_alerts(date(2026, 6, 4), inv_session)
    assert f"invoice:draft:{inv_id}" in {a.alert_key for a in alerts}


def test_sweep_handles_leap_year_february(inv_session: Session) -> None:
    # 2028 is a leap year; Feb 29 is the last day.
    alerts = compute_invoice_alerts(date(2028, 2, 29), inv_session)
    assert "invoice:sweep:2028-02" in {a.alert_key for a in alerts}


def test_draft_invoice_all_dates_null_is_skipped(inv_session: Session) -> None:
    # A draft with due=None, submitted=None (service_period_end default None) must not fire.
    inv_id = _make_draft(inv_session, number="202606-005", due=None, submitted=None)
    alerts = compute_invoice_alerts(date(2026, 6, 10), inv_session)
    assert f"invoice:draft:{inv_id}" not in {a.alert_key for a in alerts}


def test_draft_body_contains_customer_name_and_days_outstanding(inv_session: Session) -> None:
    inv_id = _make_draft(inv_session, number="202606-006", due="2026-06-01", submitted=None)
    alerts = compute_invoice_alerts(date(2026, 6, 5), inv_session)
    alert = next(a for a in alerts if a.alert_key == f"invoice:draft:{inv_id}")
    assert "Cardinal Health" in alert.body_text
    assert "days outstanding" in alert.body_text
    assert "sparkry" in alert.body_text
    assert "100" in alert.body_text
    assert "2026-06-01" in alert.body_text  # scheduled/reminder date rendered


def test_draft_invoice_unparseable_date_is_skipped_not_fatal(inv_session: Session) -> None:
    # A draft holding a non-ISO date string must be skipped, never crash dispatch.
    inv_id = _make_draft(inv_session, number="202606-007", due="06/05/2026", submitted=None)
    alerts = compute_invoice_alerts(date(2026, 6, 10), inv_session)
    assert f"invoice:draft:{inv_id}" not in {a.alert_key for a in alerts}


def test_sweep_body_lists_recurring_billers(inv_session: Session) -> None:
    alerts = compute_invoice_alerts(date(2026, 6, 30), inv_session)
    sweep = next(a for a in alerts if a.alert_key == "invoice:sweep:2026-06")
    assert "Cardinal Health" in sweep.body_text
    assert "How To Fascinate" in sweep.body_text
