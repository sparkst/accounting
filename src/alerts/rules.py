"""Pure alert-rule functions.

Given a date (and, for invoices, a Session), return the list of Alert objects
that should fire today.  No network, no writes — fully unit-testable by passing
a fixed `today`.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from src.models.invoice import Customer, Invoice

logger = logging.getLogger(__name__)

DOR_LOGIN_URL = "https://secure.dor.wa.gov/home/Login"
SPARKRY_DOR_ACCOUNT = "605-965-107"
BLACKLINE_DOR_ACCOUNT = "605-922-410"

INVOICE_SWEEP_URL = "https://books.sparkry.ai/invoices"
INVOICE_DETAIL_URL = "https://books.sparkry.ai/invoices/{invoice_id}"

RECURRING_BILLERS = (
    "Cardinal Health (SAP flat-rate, monthly)",
    "How To Fascinate (calendar-based, hourly)",
)


def _safe(s: str) -> str:
    return s.replace("\r", "").replace("\n", " ")

# Reminder days within the relevant month.
SPARKRY_REMINDER_DAYS = (3, 10, 17, 25)
BLACKLINE_REMINDER_DAYS = (3, 10, 17, 24)  # plus the due date (last day), added below


@dataclass(frozen=True)
class Alert:
    alert_key: str
    occurrence_date: str
    alert_type: str  # "tax_bo" | "invoice_sweep" | "invoice_draft"
    entity: str
    subject: str
    body_text: str
    due_date: str | None
    action_url: str
    body_html: str | None = None  # last-with-default intentionally; spec §3.1 matches


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _sparkry_monthly_alert(today: date) -> Alert | None:
    if today.day not in SPARKRY_REMINDER_DAYS:
        return None
    # Filing period = the prior month.
    first_of_month = today.replace(day=1)
    prior = first_of_month - timedelta(days=1)
    period_key = prior.strftime("%Y-%m")
    period_label = prior.strftime("%B %Y")
    due = today.replace(day=25)
    body = (
        f"Sparkry LLC monthly WA B&O for {period_label} is due {due.isoformat()}.\n"
        f"DOR account {SPARKRY_DOR_ACCOUNT}.\n"
        f"File at: {DOR_LOGIN_URL}\n"
        f"(Log in, then open the {period_label} return for account "
        f"{SPARKRY_DOR_ACCOUNT}.)"
    )
    return Alert(
        alert_key=f"tax:sparkry:bo:{period_key}",
        occurrence_date=today.isoformat(),
        alert_type="tax_bo",
        entity="sparkry",
        subject=f"WA B&O due — Sparkry LLC ({period_label}) by {due.isoformat()}",
        body_text=body,
        due_date=due.isoformat(),
        action_url=DOR_LOGIN_URL,
    )


# due-month -> (quarter label, quarter number, year offset relative to today.year)
_BLACKLINE_DUE_MONTHS = {
    4: ("Q1 (Jan-Mar)", 1, 0),
    7: ("Q2 (Apr-Jun)", 2, 0),
    10: ("Q3 (Jul-Sep)", 3, 0),
    1: ("Q4 (Oct-Dec)", 4, -1),
}


def _blackline_quarterly_alert(today: date) -> Alert | None:
    meta = _BLACKLINE_DUE_MONTHS.get(today.month)
    if meta is None:
        return None
    label, qnum, year_offset = meta
    last = _last_day(today.year, today.month)
    reminder_days = set(BLACKLINE_REMINDER_DAYS) | {last}
    if today.day not in reminder_days:
        return None
    quarter_year = today.year + year_offset
    period_key = f"{quarter_year}-Q{qnum}"
    due = date(today.year, today.month, last)
    body = (
        f"BlackLine MTB Apparel LLC quarterly WA B&O for {period_key} "
        f"{label} is due {due.isoformat()}.\n"
        f"DOR account {BLACKLINE_DOR_ACCOUNT}.\n"
        f"File at: {DOR_LOGIN_URL}\n"
        f"(Log in, then open the {period_key} return for account "
        f"{BLACKLINE_DOR_ACCOUNT}.)"
    )
    return Alert(
        alert_key=f"tax:blackline:bo:{period_key}",
        occurrence_date=today.isoformat(),
        alert_type="tax_bo",
        entity="blackline",
        subject=f"WA B&O due — BlackLine ({period_key}) by {due.isoformat()}",
        body_text=body,
        due_date=due.isoformat(),
        action_url=DOR_LOGIN_URL,
    )


def compute_tax_alerts(today: date) -> list[Alert]:
    """Return all WA B&O tax alerts that fire on `today`."""
    alerts: list[Alert] = []
    for builder in (_sparkry_monthly_alert, _blackline_quarterly_alert):
        alert = builder(today)
        if alert is not None:
            alerts.append(alert)
    return alerts


def _invoice_sweep_alert(today: date) -> Alert | None:
    if today.day != _last_day(today.year, today.month):
        return None
    month_key = today.strftime("%Y-%m")
    month_label = today.strftime("%B %Y")
    checklist = "\n".join(f"  - {b}" for b in RECURRING_BILLERS)
    body = (
        f"It's the last day of {month_label} — time to create & submit this "
        f"month's invoices.\n\nRecurring billers to check:\n{checklist}\n\n"
        f"Open invoicing: {INVOICE_SWEEP_URL}"
    )
    return Alert(
        alert_key=f"invoice:sweep:{month_key}",
        occurrence_date=today.isoformat(),
        alert_type="invoice_sweep",
        entity="all",
        subject=f"Time to create & submit {month_label} invoices",
        body_text=body,
        due_date=today.isoformat(),
        action_url=INVOICE_SWEEP_URL,
    )


def _draft_reminder_date(invoice: Invoice) -> str | None:
    return invoice.due_date or invoice.submitted_date or invoice.service_period_end


def _invoice_draft_alerts(today: date, session: Session) -> list[Alert]:
    drafts = session.query(Invoice).filter(Invoice.status == "draft").all()
    out: list[Alert] = []
    today_iso = today.isoformat()
    for inv in drafts:
        reminder_date = _draft_reminder_date(inv)
        if reminder_date is None or today_iso < reminder_date[:10]:
            continue
        try:
            reminder_dt = date.fromisoformat(reminder_date[:10])
        except ValueError:
            logger.warning(
                "invoice %s has unparseable reminder_date %r — skipping",
                inv.id, reminder_date,
            )
            continue
        customer = session.get(Customer, inv.customer_id)
        customer_name = customer.name if customer is not None else "(unknown customer)"
        days_outstanding = (today - reminder_dt).days
        action_url = INVOICE_DETAIL_URL.format(invoice_id=inv.id)
        body = (
            f"Draft invoice {_safe(inv.invoice_number)} for {_safe(customer_name)} "
            f"({_safe(inv.entity)}) totaling ${inv.total} has not been sent.\n"
            f"Scheduled date: {reminder_date} ({days_outstanding} days outstanding).\n"
            f"Open it: {action_url}"
        )
        out.append(
            Alert(
                alert_key=f"invoice:draft:{inv.id}",
                occurrence_date=today_iso,
                alert_type="invoice_draft",
                entity=inv.entity,
                subject=f"Draft invoice {_safe(inv.invoice_number)} still needs to be sent",
                body_text=body,
                due_date=reminder_date,
                action_url=action_url,
            )
        )
    return out


def compute_invoice_alerts(today: date, session: Session) -> list[Alert]:
    """Return all invoice-submission alerts that fire on `today`."""
    alerts: list[Alert] = []
    sweep = _invoice_sweep_alert(today)
    if sweep is not None:
        alerts.append(sweep)
    alerts.extend(_invoice_draft_alerts(today, session))
    return alerts
