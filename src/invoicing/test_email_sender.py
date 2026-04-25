"""Tests for src/invoicing/email_sender.py.

Verifies invoice email sending via Resend: correct from/subject/html/text/attachment,
email validation, and error propagation.

Uses mock objects for Invoice, Customer, and InvoiceLineItem to avoid DB dependency.
"""

from __future__ import annotations

import base64
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.invoicing.email_sender import send_invoice_email

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def invoice():
    return SimpleNamespace(
        id="inv-001",
        invoice_number="202604-001",
        total=Decimal("1500.00"),
        service_period_start="2026-04-01",
        service_period_end="2026-04-30",
        due_date="2026-05-14",
        submitted_date="2026-04-24",
    )


@pytest.fixture()
def customer():
    return SimpleNamespace(
        name="Acme Corp",
        contact_name="Jane Doe",
        contact_email="jane@acme.com",
    )


@pytest.fixture()
def line_items():
    return [
        SimpleNamespace(
            description="AI Engineering Coaching — Apr 7",
            quantity=Decimal("1.5"),
            unit_price=Decimal("100.00"),
            total_price=Decimal("150.00"),
            date="2026-04-07",
        ),
        SimpleNamespace(
            description="AI Engineering Coaching — Apr 14",
            quantity=Decimal("2.0"),
            unit_price=Decimal("100.00"),
            total_price=Decimal("200.00"),
            date="2026-04-14",
        ),
    ]


@pytest.fixture()
def pdf_bytes():
    return b"%PDF-1.4 fake pdf content here"


@pytest.fixture()
def payment_link_url():
    return "https://buy.stripe.com/test_abc123"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@patch.dict("os.environ", {"RESEND_API_KEY": "re_test_fake_key"})
@patch("src.invoicing.email_sender.resend")
class TestSendInvoiceEmail:
    """All tests mock the resend module at the email_sender level."""

    def test_happy_path(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """send_invoice_email calls resend.Emails.send and returns the message ID."""
        mock_resend.Emails.send.return_value = {"id": "msg_123"}

        result = send_invoice_email(
            invoice=invoice,
            line_items=line_items,
            customer=customer,
            pdf_bytes=pdf_bytes,
            payment_link_url=payment_link_url,
            to_email="jane@acme.com",
        )

        assert result == "msg_123"
        mock_resend.Emails.send.assert_called_once()

    def test_from_address(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """From address is 'Sparkry AI LLC <orders@sparkry.ai>'."""
        mock_resend.Emails.send.return_value = {"id": "msg_123"}

        send_invoice_email(
            invoice=invoice,
            line_items=line_items,
            customer=customer,
            pdf_bytes=pdf_bytes,
            payment_link_url=payment_link_url,
            to_email="jane@acme.com",
        )

        params = mock_resend.Emails.send.call_args[0][0]
        assert params["from"] == "Sparkry AI LLC <travis@sparkry.ai>"

    def test_subject_format(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """Subject line follows 'Invoice {number} from Sparkry AI LLC' format."""
        mock_resend.Emails.send.return_value = {"id": "msg_123"}

        send_invoice_email(
            invoice=invoice,
            line_items=line_items,
            customer=customer,
            pdf_bytes=pdf_bytes,
            payment_link_url=payment_link_url,
            to_email="jane@acme.com",
        )

        params = mock_resend.Emails.send.call_args[0][0]
        assert params["subject"] == "Invoice 202604-001 from Sparkry AI LLC"

    def test_html_contains_payment_link(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """HTML body contains the payment link URL in an anchor tag."""
        mock_resend.Emails.send.return_value = {"id": "msg_123"}

        send_invoice_email(
            invoice=invoice,
            line_items=line_items,
            customer=customer,
            pdf_bytes=pdf_bytes,
            payment_link_url=payment_link_url,
            to_email="jane@acme.com",
        )

        params = mock_resend.Emails.send.call_args[0][0]
        html = params["html"]
        assert payment_link_url in html
        assert f'href="{payment_link_url}"' in html

    def test_html_contains_summary(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """HTML body contains invoice amount, service period, and due date."""
        mock_resend.Emails.send.return_value = {"id": "msg_123"}

        send_invoice_email(
            invoice=invoice,
            line_items=line_items,
            customer=customer,
            pdf_bytes=pdf_bytes,
            payment_link_url=payment_link_url,
            to_email="jane@acme.com",
        )

        params = mock_resend.Emails.send.call_args[0][0]
        html = params["html"]
        assert "$1,500.00" in html
        assert "04/01/2026" in html
        assert "04/30/2026" in html
        assert "05/14/2026" in html

    def test_pdf_attachment(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """Attachments include PDF with correct filename and inline logo."""
        mock_resend.Emails.send.return_value = {"id": "msg_123"}

        send_invoice_email(
            invoice=invoice,
            line_items=line_items,
            customer=customer,
            pdf_bytes=pdf_bytes,
            payment_link_url=payment_link_url,
            to_email="jane@acme.com",
        )

        params = mock_resend.Emails.send.call_args[0][0]
        attachments = params["attachments"]
        pdf_att = attachments[0]
        assert pdf_att["filename"] == "Invoice-202604-001.pdf"
        assert pdf_att["content"] == list(pdf_bytes)
        logo_att = [a for a in attachments if a.get("content_id") == "sparkry-logo"]
        assert len(logo_att) == 1
        assert logo_att[0]["filename"] == "sparkry-logo.png"

    def test_plain_text_fallback(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """The send params include a 'text' key with a plain text version."""
        mock_resend.Emails.send.return_value = {"id": "msg_123"}

        send_invoice_email(
            invoice=invoice,
            line_items=line_items,
            customer=customer,
            pdf_bytes=pdf_bytes,
            payment_link_url=payment_link_url,
            to_email="jane@acme.com",
        )

        params = mock_resend.Emails.send.call_args[0][0]
        assert "text" in params
        text = params["text"]
        assert isinstance(text, str)
        assert len(text) > 0
        # Plain text should contain key info
        assert "202604-001" in text
        assert "$1,500.00" in text
        assert payment_link_url in text

    def test_invalid_email_no_at(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """Email without @ symbol raises ValueError before calling Resend."""
        with pytest.raises(ValueError, match="[Ii]nvalid email"):
            send_invoice_email(
                invoice=invoice,
                line_items=line_items,
                customer=customer,
                pdf_bytes=pdf_bytes,
                payment_link_url=payment_link_url,
                to_email="jane-at-acme.com",
            )

        mock_resend.Emails.send.assert_not_called()

    def test_invalid_email_no_tld(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """Email without a TLD raises ValueError before calling Resend."""
        with pytest.raises(ValueError, match="[Ii]nvalid email"):
            send_invoice_email(
                invoice=invoice,
                line_items=line_items,
                customer=customer,
                pdf_bytes=pdf_bytes,
                payment_link_url=payment_link_url,
                to_email="jane@acme",
            )

        mock_resend.Emails.send.assert_not_called()

    def test_invalid_email_with_newline(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """Email containing newline characters is rejected."""
        with pytest.raises(ValueError, match="[Ii]nvalid email"):
            send_invoice_email(
                invoice=invoice,
                line_items=line_items,
                customer=customer,
                pdf_bytes=pdf_bytes,
                payment_link_url=payment_link_url,
                to_email="jane@acme.com\r\nBcc:evil@attacker.com",
            )

        mock_resend.Emails.send.assert_not_called()

    def test_resend_error(
        self, mock_resend, invoice, line_items, customer, pdf_bytes, payment_link_url
    ):
        """When resend.Emails.send raises an exception, it propagates."""
        mock_resend.Emails.send.side_effect = RuntimeError("Resend API down")

        with pytest.raises(RuntimeError, match="Resend API down"):
            send_invoice_email(
                invoice=invoice,
                line_items=line_items,
                customer=customer,
                pdf_bytes=pdf_bytes,
                payment_link_url=payment_link_url,
                to_email="jane@acme.com",
            )
