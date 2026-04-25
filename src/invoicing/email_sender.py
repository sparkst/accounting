"""Invoice email sender using Resend.

Builds an HTML email with inline CSS (no <style> tags -- email clients strip
them) and table-based layout for Outlook compatibility.  Attaches the invoice
PDF and includes a plain-text fallback.
"""

from __future__ import annotations

import html as _html
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import resend

_LOGO_PATH = Path(__file__).parent / "assets" / "sparkry-logo.png"

FROM_ADDRESS = "Sparkry AI LLC <travis@sparkry.ai>"

_FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
)

_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)*"
    r"\.[a-zA-Z]{2,}$"
)


def _validate_email(email: str) -> None:
    """Raise ValueError if *email* is not a valid user@domain.tld address."""
    if "\r" in email or "\n" in email:
        raise ValueError(f"Invalid email address (contains forbidden characters): {email!r}")
    if not _EMAIL_RE.match(email):
        raise ValueError(f"Invalid email address: {email!r}")


def _format_currency(amount: Decimal | float | int) -> str:
    """Format a number as $X,XXX.XX."""
    return f"${Decimal(str(amount)):,.2f}"


def _format_date(d: str | date) -> str:
    """Format a date as MM/DD/YYYY."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%m/%d/%Y")


def _build_html(
    invoice: Any,
    line_items: list[Any],
    customer: Any,
    payment_link_url: str,
) -> str:
    """Build an HTML email body with inline CSS and table layout."""
    amount_str = _format_currency(invoice.total)
    safe_number = _html.escape(str(invoice.invoice_number))
    safe_name = _html.escape(str(customer.contact_name)) if customer.contact_name else ""
    safe_link = _html.escape(payment_link_url)
    greeting = f"Hi {safe_name}," if customer.contact_name else "Hello,"

    f = _FONT_STACK

    li_rows = ""
    for item in line_items:
        safe_desc = _html.escape(str(item.description))
        safe_qty = _html.escape(str(item.quantity))
        li_rows += (
            f'<tr style="border-bottom: 1px solid #eeeeee;">'
            f'<td style="padding: 8px 12px; font-family: {f}; font-size: 14px; color: #333333;">{safe_desc}</td>'
            f'<td style="padding: 8px 12px; font-family: {f}; font-size: 14px; color: #333333; text-align: center;">{safe_qty}</td>'
            f'<td style="padding: 8px 12px; font-family: {f}; font-size: 14px; color: #333333; text-align: right;">{_format_currency(item.unit_price)}</td>'
            f'<td style="padding: 8px 12px; font-family: {f}; font-size: 14px; color: #333333; text-align: right;">{_format_currency(item.total_price)}</td>'
            f"</tr>"
        )

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Invoice {safe_number}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f5f5f7;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color: #f5f5f7;">
<tr>
<td align="center" style="padding: 40px 20px;">

<!-- Main container -->
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="background-color: #ffffff; border-radius: 8px; overflow: hidden;">

<!-- Header -->
<tr>
<td style="background-color: #ffffff; padding: 24px 32px; border-bottom: 1px solid #e5e5e5;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
<tr>
<td><img src="cid:sparkry-logo" alt="Sparkry AI LLC" width="160" style="display: block; height: auto;"></td>
<td align="right" style="font-family: {f}; font-size: 14px; color: #86868b;">Invoice {safe_number}</td>
</tr>
</table>
</td>
</tr>

<!-- Greeting -->
<tr>
<td style="padding: 32px 32px 16px 32px; font-family: {f}; font-size: 16px; color: #1d1d1f;">
{greeting}
</td>
</tr>
<tr>
<td style="padding: 0 32px 24px 32px; font-family: {f}; font-size: 14px; color: #333333; line-height: 1.5;">
Please find attached invoice <strong>{safe_number}</strong> for your review.
</td>
</tr>

<!-- Invoice summary table -->
<tr>
<td style="padding: 0 32px 24px 32px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color: #f5f5f7; border-radius: 8px;">
<tr>
<td style="padding: 16px 20px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
<tr>
<td style="padding: 4px 0; font-family: {f}; font-size: 13px; color: #86868b;">Invoice Number</td>
<td align="right" style="padding: 4px 0; font-family: {f}; font-size: 14px; color: #1d1d1f; font-weight: 500;">{safe_number}</td>
</tr>
<tr>
<td style="padding: 4px 0; font-family: {f}; font-size: 13px; color: #86868b;">Amount Due</td>
<td align="right" style="padding: 4px 0; font-family: {f}; font-size: 14px; color: #1d1d1f; font-weight: 600;">{amount_str}</td>
</tr>
<tr>
<td style="padding: 4px 0; font-family: {f}; font-size: 13px; color: #86868b;">Service Period</td>
<td align="right" style="padding: 4px 0; font-family: {f}; font-size: 14px; color: #1d1d1f;">{_format_date(invoice.service_period_start)} to {_format_date(invoice.service_period_end)}</td>
</tr>
<tr>
<td style="padding: 4px 0; font-family: {f}; font-size: 13px; color: #86868b;">Due Date</td>
<td align="right" style="padding: 4px 0; font-family: {f}; font-size: 14px; color: #1d1d1f; font-weight: 500;">{_format_date(invoice.due_date)}</td>
</tr>
</table>
</td>
</tr>
</table>
</td>
</tr>

<!-- Line items table -->
<tr>
<td style="padding: 0 32px 24px 32px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border: 1px solid #e5e5e5; border-radius: 6px; border-collapse: collapse;">
<tr style="background-color: #f5f5f7;">
<td style="padding: 10px 12px; font-family: {f}; font-size: 12px; font-weight: 600; color: #86868b; text-transform: uppercase; letter-spacing: 0.5px;">Description</td>
<td style="padding: 10px 12px; font-family: {f}; font-size: 12px; font-weight: 600; color: #86868b; text-transform: uppercase; letter-spacing: 0.5px; text-align: center;">Qty</td>
<td style="padding: 10px 12px; font-family: {f}; font-size: 12px; font-weight: 600; color: #86868b; text-transform: uppercase; letter-spacing: 0.5px; text-align: right;">Rate</td>
<td style="padding: 10px 12px; font-family: {f}; font-size: 12px; font-weight: 600; color: #86868b; text-transform: uppercase; letter-spacing: 0.5px; text-align: right;">Amount</td>
</tr>
{li_rows}
<tr style="background-color: #f5f5f7;">
<td colspan="3" style="padding: 10px 12px; font-family: {f}; font-size: 14px; font-weight: 600; color: #1d1d1f; text-align: right;">Total</td>
<td style="padding: 10px 12px; font-family: {f}; font-size: 14px; font-weight: 600; color: #1d1d1f; text-align: right;">{amount_str}</td>
</tr>
</table>
</td>
</tr>

<!-- CTA Button -->
<tr>
<td align="center" style="padding: 8px 32px 32px 32px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0">
<tr>
<td align="center" style="background-color: #0071e3; border-radius: 8px;">
<a href="{safe_link}" target="_blank" style="display: inline-block; padding: 14px 32px; font-family: {f}; font-size: 16px; font-weight: 500; color: #ffffff; text-decoration: none;">Pay Invoice</a>
</td>
</tr>
</table>
</td>
</tr>

<!-- Footer -->
<tr>
<td style="padding: 20px 32px; background-color: #f5f5f7; border-top: 1px solid #e5e5e5;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
<tr>
<td style="font-family: {f}; font-size: 12px; color: #86868b; line-height: 1.5;">
Sparkry AI LLC<br>
travis@sparkry.com<br>
<br>
A PDF copy of this invoice is attached for your records.
</td>
</tr>
</table>
</td>
</tr>

</table>
<!-- /Main container -->

</td>
</tr>
</table>
</body>
</html>"""

    return html


def _build_plain_text(
    invoice: Any,
    line_items: list[Any],
    customer: Any,
    payment_link_url: str,
) -> str:
    """Build a plain-text fallback for the invoice email."""
    amount_str = _format_currency(invoice.total)
    greeting = f"Hi {customer.contact_name}," if customer.contact_name else "Hello,"

    lines = [
        greeting,
        "",
        f"Please find attached invoice {invoice.invoice_number} for your review.",
        "",
        "--- Invoice Summary ---",
        f"Invoice Number:  {invoice.invoice_number}",
        f"Amount Due:      {amount_str}",
        f"Service Period:  {_format_date(invoice.service_period_start)} to {_format_date(invoice.service_period_end)}",
        f"Due Date:        {_format_date(invoice.due_date)}",
        "",
        "--- Line Items ---",
    ]

    for item in line_items:
        lines.append(
            f"  {item.description}  |  Qty: {item.quantity}  |  "
            f"Rate: {_format_currency(item.unit_price)}  |  "
            f"Amount: {_format_currency(item.total_price)}"
        )

    lines.extend(
        [
            "",
            f"Total: {amount_str}",
            "",
            f"Pay online: {payment_link_url}",
            "",
            "---",
            "Sparkry AI LLC",
            "travis@sparkry.com",
        ]
    )

    return "\n".join(lines)


def send_invoice_email(
    invoice: Any,
    line_items: list[Any],
    customer: Any,
    pdf_bytes: bytes,
    payment_link_url: str,
    to_email: str,
) -> str:
    """Send an invoice email via Resend and return the Resend message ID."""
    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend.api_key:
        raise ValueError("RESEND_API_KEY is not configured")

    _validate_email(to_email)

    subject = f"Invoice {invoice.invoice_number} from Sparkry AI LLC"
    html_body = _build_html(invoice, line_items, customer, payment_link_url)
    plain_text = _build_plain_text(invoice, line_items, customer, payment_link_url)

    filename = f"Invoice-{invoice.invoice_number}.pdf"

    attachments = [
        {"filename": filename, "content": list(pdf_bytes)},
    ]
    if _LOGO_PATH.exists():
        logo_bytes = _LOGO_PATH.read_bytes()
        attachments.append({
            "filename": "sparkry-logo.png",
            "content": list(logo_bytes),
            "content_type": "image/png",
            "content_id": "sparkry-logo",
        })

    params: resend.Emails.SendParams = {
        "from": FROM_ADDRESS,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": plain_text,
        "attachments": attachments,
    }

    result = resend.Emails.send(params)
    return result["id"]
