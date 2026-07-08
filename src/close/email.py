"""Monthly-close email: inline-CSS HTML render + Resend send + ledger (spec §1.4).

``render_html`` is a pure function of the CloseReport so tests assert content
without sending. ``send_close_report`` sends via Resend (DRY-RUN default) and,
only on ``apply``, records one ``alert_dispatch`` row with
``alert_type="monthly_close"``, ``delivery_channel="resend_email"``,
``payload_json=NULL`` — keeping monthly-close out of the REQ-FIX-ALR-002
webhook-only replay sweep (a failed send is re-run against fresher data).
"""

from __future__ import annotations

import html as _html
import os
from datetime import date

from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.close.report import (
    CloseReport,
    account_link,
    needs_review_link,
    vendor_link,
)
from src.invoicing.email_sender import _FONT_STACK, _format_currency, _validate_email
from src.utils.constants import INVOICE_FROM_ADDRESS

ALERT_TYPE = "monthly_close"
DELIVERY_CHANNEL = "resend_email"


def _esc(value: object) -> str:
    return _html.escape(str(value))


def _row(label: str, value: str) -> str:
    f = _FONT_STACK
    return (
        f'<tr><td style="padding:4px 12px;font-family:{f};font-size:13px;color:#86868b;">{label}</td>'
        f'<td style="padding:4px 12px;font-family:{f};font-size:14px;color:#1d1d1f;text-align:right;">{value}</td></tr>'
    )


def _section(title: str) -> str:
    f = _FONT_STACK
    return (
        f'<tr><td colspan="2" style="padding:20px 12px 6px 12px;font-family:{f};'
        f'font-size:15px;font-weight:600;color:#1d1d1f;border-bottom:1px solid #e5e5e5;">{title}</td></tr>'
    )


def _line(text: str) -> str:
    f = _FONT_STACK
    return (
        f'<tr><td colspan="2" style="padding:4px 12px;font-family:{f};font-size:13px;'
        f'color:#333333;line-height:1.5;">{text}</td></tr>'
    )


def _link(url: str, text: str = "view") -> str:
    return f'<a href="{_esc(url)}" style="color:#0071e3;text-decoration:none;">{_esc(text)}</a>'


def render_html(report: CloseReport, *, narrative: str | None = None) -> str:
    """Render the close report as a self-contained inline-CSS HTML email body."""
    f = _FONT_STACK
    m = report.month
    parts: list[str] = []

    # Header KPIs
    parts.append(_section(f"Monthly Close — {_esc(m)}"))
    if narrative:
        parts.append(_line(f"<em>{_esc(narrative)}</em>"))
    parts.append(_row("Rows ingested", str(report.rows_ingested)))
    parts.append(_row("Auto-confirmed", str(report.autoconfirm.total)))
    parts.append(_row("Needs-review depth", str(report.needs_review_depth)))

    # Reconciliation (§1.2)
    parts.append(_section("Reconciliation"))
    rec = report.reconcile
    for item in rec.items:
        gap = f" ⚠️ {len(item.gap_days)} gap day(s)" if item.has_gap else ""
        parts.append(_line(f"<strong>{_esc(item.institution_name)}</strong> ({_esc(item.status)}){gap}"))
        for acc in item.accounts:
            pm = _esc(acc.payment_method or acc.account_name or "?")
            if acc.tie_out_ok is False:
                body = (
                    f"{pm}: {_esc(acc.note)} — "
                    f"{len(item.accounts)} acct(s) [{_link(account_link(acc.payment_method or ''))}]"
                )
            elif acc.tie_out_ok is True:
                body = f"{pm}: tie-out ok (Δ {acc.balance_delta} = Σ {acc.register_sum})"
            else:
                body = f"{pm}: {_esc(acc.note)} — {acc.register_count} row(s), Σ {acc.register_sum}"
            parts.append(_line(body))
    if not rec.items:
        parts.append(_line("No active Plaid items."))
    for sp in rec.stuck_pending:
        parts.append(_line(f"Stuck pending since {_esc(sp.date)}: {_esc(sp.description)} {_format_currency(sp.amount)}"))
    for bl in rec.needs_review_backlog:
        parts.append(
            _line(
                f"Needs-review backlog [{_esc(bl.entity)}]: {bl.count} (oldest {_esc(bl.oldest_date)}) "
                f"[{_link(needs_review_link(bl.entity))}]"
            )
        )
    for payout in rec.unmatched_payouts:
        parts.append(_line(f"Unmatched payout: {_esc(payout.date)} {_esc(payout.description)}"))
    for um in rec.unmapped_accounts:
        parts.append(_line(f"Unmapped account: {_esc(um)}"))

    # Anomalies (§1.3)
    parts.append(_section("Anomalies"))
    an = report.anomalies
    for nv in an.new_vendors:
        parts.append(
            _line(
                f"New vendor <strong>{_esc(nv.vendor_key)}</strong> [{_esc(nv.entity)}]: "
                f"{nv.count}× {_format_currency(nv.total)} [{_link(vendor_link(nv.vendor_key, m))}]"
            )
        )
    for o in an.outliers:
        parts.append(
            _line(
                f"Outlier {_esc(o.vendor_key)} {_format_currency(o.amount)} on {_esc(o.date)} "
                f"(μ {_format_currency(o.mean)}, z {o.z_score}) [{_link(vendor_link(o.vendor_key, m))}]"
            )
        )
    for mr in an.missing_recurring:
        parts.append(
            _line(
                f"Missing recurring {_esc(mr.vendor_key)} (last {_esc(mr.last_seen)}, "
                f"~{_format_currency(mr.typical_amount)}, {_esc(mr.source)})"
            )
        )
    if not (an.new_vendors or an.outliers or an.missing_recurring):
        parts.append(_line("No anomalies detected."))

    # Auto-confirm month summary (§2)
    parts.append(_section("Auto-confirm summary"))
    parts.append(_line(f"{report.autoconfirm.total} transaction(s) auto-confirmed this month."))
    for v in report.autoconfirm.by_vendor:
        parts.append(_line(f"{_esc(v.vendor)}: {v.count}× {_format_currency(v.total)}"))

    # Data hygiene (REQ-FIX-DAT-002)
    parts.append(_section("Data hygiene"))
    for callout in report.data_hygiene:
        parts.append(_line(_esc(callout)))

    body = "".join(parts)
    return (
        '<body style="margin:0;padding:0;background-color:#f5f5f7;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640" '
        'style="background-color:#ffffff;border-radius:8px;overflow:hidden;">'
        f'{body}'
        f'<tr><td colspan="2" style="padding:16px 12px;font-family:{f};font-size:11px;'
        f'color:#86868b;">Generated {_esc(report.generated_at)} · Sparkry books</td></tr>'
        "</table></td></tr></table></body>"
    )


def subject_for(report: CloseReport) -> str:
    return f"Monthly close — {report.month}"


def send_close_report(
    session: Session,
    report: CloseReport,
    *,
    apply: bool = False,
    to_email: str | None = None,
    narrative: str | None = None,
    today: date | None = None,
) -> AlertDispatch | None:
    """Render + (on ``apply``) send the close email and record the ledger row.

    DRY-RUN default: renders nothing to the wire and writes no ledger row.
    Returns the ``AlertDispatch`` row on ``apply``, else ``None``. The caller
    owns the commit.
    """
    if not apply:
        return None

    occurrence = (today or date.today()).isoformat()
    subject = subject_for(report)
    html_body = render_html(report, narrative=narrative)
    recipient = to_email or os.environ["ALERT_TO_EMAIL"]

    status = "sent"
    http_status: int | None = None
    error_detail: str | None = None
    try:
        _validate_email(recipient)
        import resend

        resend.api_key = os.environ.get("RESEND_API_KEY", "")
        if not resend.api_key:
            raise ValueError("RESEND_API_KEY is not configured")
        params: resend.Emails.SendParams = {
            "from": INVOICE_FROM_ADDRESS,
            "to": [recipient],
            "subject": subject,
            "html": html_body,
        }
        resend.Emails.send(params)
        http_status = 200
    except Exception as exc:  # noqa: BLE001 — a send failure is recorded, not raised
        status = "failed"
        error_detail = f"{type(exc).__name__}: {exc}"

    row = AlertDispatch(
        alert_key=f"close:{report.month}",
        occurrence_date=occurrence,
        alert_type=ALERT_TYPE,
        entity="all",
        subject=subject,
        status=status,
        http_status=http_status,
        error_detail=error_detail,
        payload_json=None,
        delivery_channel=DELIVERY_CHANNEL,
    )
    session.add(row)
    session.flush()
    return row
