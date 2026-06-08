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
