"""Tests for the Gmail/n8n adapter.

REQ-ID: ADAPTER-GMAIL-001  Reads JSON files and inserts Transaction rows.
REQ-ID: ADAPTER-GMAIL-002  Extracts vendor, date, amount correctly.
REQ-ID: ADAPTER-GMAIL-003  Deduplicates: running twice yields no new rows.
REQ-ID: ADAPTER-GMAIL-004  Attachments linked by hex-ID prefix.
REQ-ID: ADAPTER-GMAIL-005  raw_data preserved verbatim.
REQ-ID: ADAPTER-GMAIL-006  Forwarded emails: real vendor extracted from body.
REQ-ID: ADAPTER-GMAIL-007  Unknown amounts stored as None (not $0.00).

All tests use in-memory SQLite and fixture JSON files written to a temp dir.
"""

from __future__ import annotations

import decimal
import json
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.gmail_n8n import (
    GmailN8nAdapter,
    _extract_forwarded_vendor,
    _is_self_forwarded,
    extract_amount,
    extract_vendor,
    find_attachments,
    normalise_date,
)
from src.models.base import Base
from src.models.enums import FileStatus, Source, TransactionStatus
from src.models.ingested_file import IngestedFile
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# Fixtures — DB session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite session per test function."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Fixtures — JSON payloads
# ---------------------------------------------------------------------------

# Realistic payloads based on real n8n output.

ANTHROPIC_RECEIPT: dict[str, object] = {
    "id": "19578f6fd72939df",
    "filename": "2025-03-09_Anthropic_PBC_19578f6fd72939df",
    "date": "2025-03-09T03:33:26.000Z",
    "from": "Anthropic, PBC <invoice+statements@mail.anthropic.com>",
    "subject": "Your receipt from Anthropic, PBC #2355-2148",
    "body_text": (
        "Anthropic, PBC  (https://www.anthropic.com/)\n\n"
        "Anthropic, PBC\n\n"
        "Receipt from Anthropic, PBC $238.03 Paid March 8, 2025\n"
        "Receipt #2355-2148 Mar 8, 2025 – Mar 8, 2026 Claude Pro Qty 1 $216.00 "
        "Subtotal $216.00 Total excluding tax $216.00 Tax (10.2%) $22.03 "
        "Total $238.03 Amount paid $238.03\n"
    ),
    "body_html": "<html></html>",
}

RUNPOD_RECEIPT: dict[str, object] = {
    "id": "1962be5d8a052709",
    "filename": "2025-04-12_RunPod_1962be5d8a052709",
    "date": "2025-04-12T21:26:45.000Z",
    "from": "RunPod <receipts+acct_1KLZG6LeeX2jf1uK@stripe.com>",
    "subject": "Your RunPod receipt [#1181-8952]",
    "body_text": (
        "Receipt from RunPod Receipt #1181-8952\n"
        "Amount paid\n"
        "$10.00\n"
        "\n"
        "Date paid\n"
        "Apr 12, 2025, 5:26:42 PM\n"
        "\n"
        "- Amount paid : $10.00\n"
    ),
    "body_html": "<html></html>",
}

CLOUDFLARE_FORWARDED: dict[str, object] = {
    "id": "1962af65f562dc96",
    "filename": "2025-04-12_Travis_Sparks_1962af65f562dc96",
    "date": "2025-04-12T17:04:59.000Z",
    "from": "Travis Sparks <sparkst@gmail.com>",
    "subject": "Fwd: Your Cloudflare Invoice is Available",
    "body_text": (
        "---------- Forwarded message ---------\n"
        "From: Cloudflare <noreply@notify.cloudflare.com>\n"
        "\n"
        "Invoice ID: IN-33191763\n"
        "Due on April 12, 2025\n"
        "Invoice Amount: $11.51\n"
    ),
    "body_html": "<html></html>",
}

TUNE_UP_RECEIPT: dict[str, object] = {
    "id": "19784212f5026421",
    "filename": "2025-06-18_Travis_Sparks_19784212f5026421",
    "date": "2025-06-18T14:00:00.000Z",
    "from": "Travis Sparks <sparkst@blacklinemtb.com>",
    "subject": "Payment receipt",
    "body_text": (
        "Payment receipt\n"
        "You paid $2,025.00\n"
        "to Tune-Up Events LLC on 6/18/2025\n"
        "Invoice amount $2,000.00\n"
        "Online convenience fee $25.00\n"
        "Total $2,025.00\n"
        "Status Paid\n"
    ),
    "body_html": "<html></html>",
}

GOOGLE_WORKSPACE_INVOICE: dict[str, object] = {
    "id": "19556bbaec73446a",
    "filename": "2025-03-02_Google_Payments_19556bbaec73446a",
    "date": "2025-03-02T12:01:34.000Z",
    "from": "Google Payments <payments-noreply@google.com>",
    "subject": "Google Workspace: Your invoice is available for sparkry.com",
    "body_text": (
        "Google Workspace\n\n"
        "Your Google Workspace monthly invoice is available.\n"
        "Domain sparkry.com\n"
        "Invoice number 5189167118\n"
        # No dollar amount — should flag needs_review
    ),
    "body_html": "<html></html>",
}

NO_PLAIN_TEXT: dict[str, object] = {
    "id": "194b3e18b97928ed",
    "filename": "2025-01-29_Fiverr_194b3e18b97928ed",
    "date": "2025-01-29T21:04:49.000Z",
    "from": "Fiverr <noreply@e.fiverr.com>",
    "subject": "Here's your receipt of doing",
    "body_text": "No plain text body available.",
    "body_html": "<html></html>",
}

RENDER_RECEIPT: dict[str, object] = {
    "id": "19690c7bdbef845a",
    "filename": "2025-05-02_Render_19690c7bdbef845a",
    "date": "2025-05-02T08:00:00.000Z",
    "from": "Render <invoice+statements@render.com>",
    "subject": "Your Render receipt",
    "body_text": (
        "Render (https://render.com)\n\n"
        "Receipt from Render $14.66 Paid May 2, 2025\n"
        "Subtotal $14.66 Total $14.66 Amount paid $14.66\n"
    ),
    "body_html": "<html></html>",
}

GOOGLE_PAYMENT_RECEIVED: dict[str, object] = {
    "id": "198668f5e0dbb17e",
    "filename": "2025-08-01_Google_Payments_198668f5e0dbb17e",
    "date": "2025-08-01T00:00:00.000Z",
    "from": "Google Payments <payments-noreply@google.com>",
    "subject": "Google Cloud: Payment received",
    "body_text": (
        "Google Cloud Platform\n\n"
        "Payment received\n\n"
        "Your payment amount of $5.63 to Google was received on Aug 1, 2025.\n"
    ),
    "body_html": "<html></html>",
}

ISSAQUAH_RECEIPT: dict[str, object] = {
    "id": "199641b24ceaa028",
    "filename": "2025-09-19_issaquah_199641b24ceaa028",
    "date": "2025-09-19T00:00:00.000Z",
    "from": "issaquah <billing@issaquah.example.com>",
    "subject": "Invoice paid",
    "body_text": (
        "WE'VE RECEIVED YOUR PAYMENT OF $891.07 FOR INVOICE #95945."
    ),
    "body_html": "<html></html>",
}

WIFI_ONBOARD_DIRECT: dict[str, object] = {
    "id": "19670461f10ff086",
    "filename": "2025-04-26_Wi-Fi_Onboard_19670461f10ff086",
    "date": "2025-04-26T18:00:00.000Z",
    "from": "Wi-Fi Onboard <info@info.wifionboard.com>",
    "subject": "Here's Your Wi-Fi Onboard Receipt",
    # Wi-Fi Onboard body is mostly HTML tracking URLs with no dollar amount
    "body_text": (
        "Web browser\n"
        "[https://info.wifionboard.com/pub/cc?_ri_=...]\n"
        "Wifi Onboard\n"
        "[https://info.wifionboard.com/assets/...]\n"
        "Alaska Airlines\n"
        "Thanks for your purchase!\n"
        "Customer: Traveler\n"
        "Email Address: travis+accounting@sparkry.com\n"
        "Order: 413495827SSAS\n"
    ),
    "body_html": "<html></html>",
}

FORWARDED_WIFI_ONBOARD: dict[str, object] = {
    "id": "1962ca5949f79ea2",
    "filename": "2025-04-13_Travis_Sparks_1962ca5949f79ea2",
    "date": "2025-04-13T00:00:00.000Z",
    "from": "Travis Sparks <sparkst@gmail.com>",
    "subject": "Fwd: Here's Your Wi-Fi Onboard Receipt",
    "body_text": (
        "---------- Forwarded message ---------\n"
        "From: Wi-Fi Onboard <info@info.wifionboard.com>\n"
        "Date: Sat, Apr 12, 2025 at 4:27 PM\n"
        "\n"
        "Total paid\n"
        " $ 8.00 \n"
    ),
    "body_html": "<html></html>",
}

OBJECT_OBJECT_RECEIPT: dict[str, object] = {
    # accounting#85: a JS object serialized before it reaches gmail_n8n.py,
    # producing a `from` header whose display name is the literal string
    # "[object Object]" — extract_vendor() then stores that literal as the
    # description, defeating vendor/pattern matching and (per the ElevenLabs
    # regression, 4ed8f5dd-f936-4357-8a58-7e47b9f3af8f) letting tier-3
    # mis-guess income and inflate B&O gross.
    "id": "4ed8f5dd1936",
    "filename": "2026-08-18_object_object_4ed8f5dd1936",
    "date": "2026-08-18T00:00:00.000Z",
    "from": "[object Object] <billing@mail.elevenlabs.io>",
    "subject": "Your receipt",
    "body_text": "Receipt Amount paid $24.27\n",
    "body_html": "<html></html>",
}

FORWARDED_APPLE_RECEIPT: dict[str, object] = {
    "id": "196dff7d8e138b25",
    "filename": "2025-05-17_Travis_Sparks_196dff7d8e138b25",
    "date": "2025-05-17T00:00:00.000Z",
    "from": "Travis Sparks <sparkst@gmail.com>",
    "subject": "Fwd: Your receipt from Apple.",
    "body_text": (
        "---------- Forwarded message ---------\n"
        "From: Apple <no_reply@email.apple.com>\n"
        "Date: Sat, May 17, 2025 at 9:02 AM\n"
        "\n"
        "Your Apple Account was used to purchase Monthly Subscription.\n"
        "Amount paid $2.99\n"
    ),
    "body_html": "<html></html>",
}


# ---------------------------------------------------------------------------
# Helper: write fixture JSON file to tmp_path
# ---------------------------------------------------------------------------


def write_fixture(tmp_path: Path, payload: dict[str, object]) -> Path:
    """Write a single fixture as an n8n-style JSON array file."""
    filename = str(payload["filename"]) + ".json"
    p = tmp_path / filename
    p.write_text(json.dumps([payload]), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Unit tests: extract_vendor
# ---------------------------------------------------------------------------


class TestExtractVendor:
    def test_display_name_extracted(self) -> None:
        assert extract_vendor("Anthropic, PBC <invoice+statements@mail.anthropic.com>") == "Anthropic, PBC"

    def test_simple_name(self) -> None:
        assert extract_vendor("Fiverr <noreply@e.fiverr.com>") == "Fiverr"

    def test_no_display_name_returns_raw(self) -> None:
        assert extract_vendor("payments-noreply@google.com") == "payments-noreply@google.com"

    def test_strips_whitespace(self) -> None:
        assert extract_vendor("  RunPod  <receipts@stripe.com>  ") == "RunPod"

    def test_google_payments(self) -> None:
        assert extract_vendor("Google Payments <payments-noreply@google.com>") == "Google Payments"

    def test_render(self) -> None:
        assert extract_vendor("Render <invoice+statements@render.com>") == "Render"

    def test_travis_forwarded_no_body_falls_back_to_name(self) -> None:
        # No body_text → cannot resolve real vendor → fall back to display name
        assert extract_vendor("Travis Sparks <sparkst@gmail.com>") == "Travis Sparks"

    def test_travis_forwarded_with_body_extracts_real_vendor(self) -> None:
        body = (
            "---------- Forwarded message ---------\n"
            "From: Cloudflare <noreply@notify.cloudflare.com>\n"
            "\n"
            "Invoice Amount: $11.51\n"
        )
        assert extract_vendor("Travis Sparks <sparkst@gmail.com>", body) == "Cloudflare"

    def test_self_forward_sparkry_address(self) -> None:
        body = (
            "---------- Forwarded message ---------\n"
            "From: Apple <no_reply@email.apple.com>\n"
        )
        assert extract_vendor("Travis Sparks <travis@sparkry.com>", body) == "Apple"

    def test_self_forward_email_only_uses_domain(self) -> None:
        body = (
            "---------- Forwarded message ---------\n"
            "From: <noreply@dhl.com>\n"
        )
        assert extract_vendor("Travis Sparks <sparkst@gmail.com>", body) == "DHL"


# ---------------------------------------------------------------------------
# Unit tests: normalise_date
# ---------------------------------------------------------------------------


class TestNormaliseDate:
    def test_iso_with_time(self) -> None:
        assert normalise_date("2025-03-09T03:33:26.000Z") == "2025-03-09"

    def test_date_only(self) -> None:
        assert normalise_date("2025-03-09") == "2025-03-09"

    def test_too_short_raises(self) -> None:
        with pytest.raises(ValueError):
            normalise_date("2025")


# ---------------------------------------------------------------------------
# Unit tests: extract_amount
# ---------------------------------------------------------------------------


class TestExtractAmount:
    def test_amount_paid_label(self) -> None:
        body = "Amount paid $238.03\n"
        assert extract_amount(body) == decimal.Decimal("238.03")

    def test_amount_paid_with_colon_and_dash(self) -> None:
        body = "- Amount paid : $10.00\n"
        assert extract_amount(body) == decimal.Decimal("10.00")

    def test_receipt_from_pattern(self) -> None:
        body = "Receipt from Anthropic, PBC $238.03 Paid March 8, 2025\n"
        assert extract_amount(body) == decimal.Decimal("238.03")

    def test_receipt_from_render(self) -> None:
        body = "Receipt from Render $14.66 Paid May 2, 2025\n"
        assert extract_amount(body) == decimal.Decimal("14.66")

    def test_invoice_amount_label(self) -> None:
        body = "Invoice Amount: $11.51\n"
        assert extract_amount(body) == decimal.Decimal("11.51")

    def test_you_paid_with_comma(self) -> None:
        body = "You paid $2,025.00\nto Tune-Up Events LLC\n"
        assert extract_amount(body) == decimal.Decimal("2025.00")

    def test_payment_of_pattern(self) -> None:
        body = "WE'VE RECEIVED YOUR PAYMENT OF $891.07 FOR INVOICE #95945."
        assert extract_amount(body) == decimal.Decimal("891.07")

    def test_payment_amount_of_pattern(self) -> None:
        body = "Your payment amount of $5.63 to Google was received on Aug 1, 2025."
        assert extract_amount(body) == decimal.Decimal("5.63")

    def test_total_colon_pattern(self) -> None:
        body = "Subtotal: $75.03\nTax: $7.65\nTotal: $83.73\n"
        assert extract_amount(body) == decimal.Decimal("83.73")

    def test_total_standalone_line(self) -> None:
        body = "Items $22.17\nTotal $24.45\n"
        assert extract_amount(body) == decimal.Decimal("24.45")

    def test_amount_colon_pattern(self) -> None:
        body = "Amount: $947.14\nSurcharge: $27.59\n"
        assert extract_amount(body) == decimal.Decimal("947.14")

    def test_no_amount_returns_none(self) -> None:
        assert extract_amount("No plain text body available.") is None

    def test_no_amount_empty_string(self) -> None:
        assert extract_amount("") is None

    def test_prefers_amount_paid_over_total(self) -> None:
        # "Amount paid" is listed first in _AMOUNT_PATTERNS — should win over Total
        body = (
            "Subtotal $216.00 Total excluding tax $216.00 "
            "Total $238.03 Amount paid $238.03\n"
        )
        assert extract_amount(body) == decimal.Decimal("238.03")

    def test_large_comma_separated_amount(self) -> None:
        body = "Amount: $1,000.21\n"
        assert extract_amount(body) == decimal.Decimal("1000.21")


# ---------------------------------------------------------------------------
# Unit tests: find_attachments
# ---------------------------------------------------------------------------


class TestFindAttachments:
    def test_finds_pdf_attachments(self, tmp_path: Path) -> None:
        hex_id = "19578f6fd72939df"
        pdf1 = tmp_path / f"{hex_id}_Invoice-3F3E740C-0001.pdf"
        pdf2 = tmp_path / f"{hex_id}_Receipt-2355-2148.pdf"
        pdf1.touch()
        pdf2.touch()
        # JSON file should be excluded
        json_file = tmp_path / f"2025-03-09_Anthropic_PBC_{hex_id}.json"
        json_file.touch()

        found = find_attachments(tmp_path, hex_id)
        assert str(pdf1.resolve()) in found
        assert str(pdf2.resolve()) in found
        # JSON excluded from attachments
        assert not any(p.endswith(".json") for p in found)

    def test_no_attachments_returns_empty(self, tmp_path: Path) -> None:
        assert find_attachments(tmp_path, "aabbccdd") == []

    def test_does_not_return_other_hex_id(self, tmp_path: Path) -> None:
        (tmp_path / "aaaa_receipt.pdf").touch()
        result = find_attachments(tmp_path, "bbbb")
        assert result == []

    def test_returns_sorted_paths(self, tmp_path: Path) -> None:
        hex_id = "abcdef1234567890"
        for name in ["zzz.pdf", "aaa.pdf", "mmm.jpg"]:
            (tmp_path / f"{hex_id}_{name}").touch()
        found = find_attachments(tmp_path, hex_id)
        assert found == sorted(found)


# ---------------------------------------------------------------------------
# Integration tests: GmailN8nAdapter.run()
# ---------------------------------------------------------------------------


class TestGmailN8nAdapterRun:
    def _make_adapter(self, *dirs: Path) -> GmailN8nAdapter:
        return GmailN8nAdapter(source_dirs=[str(d) for d in dirs])

    # ------------------------------------------------------------------
    # Happy-path ingestion
    # ------------------------------------------------------------------

    def test_ingests_anthropic_receipt(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, ANTHROPIC_RECEIPT)
        adapter = self._make_adapter(tmp_path)
        result = adapter.run(session)

        assert result.records_created == 1
        assert result.records_processed == 1
        assert result.records_failed == 0

        tx = session.query(Transaction).one()
        assert tx.source == Source.GMAIL_N8N.value
        assert tx.source_id == "19578f6fd72939df"
        assert tx.date == "2025-03-09"
        assert tx.description == "Anthropic, PBC"
        assert tx.amount == decimal.Decimal("-238.03")
        assert tx.currency == "USD"
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value
        assert tx.raw_data == ANTHROPIC_RECEIPT

    def test_raw_data_preserved_verbatim(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, RUNPOD_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.raw_data == RUNPOD_RECEIPT
        assert tx.raw_data["subject"] == "Your RunPod receipt [#1181-8952]"

    def test_vendor_extracted_from_from_field(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, RUNPOD_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.description == "RunPod"

    def test_forwarded_email_vendor_extracted_from_body(
        self, tmp_path: Path, session: Session
    ) -> None:
        """Forwarded emails: real vendor is extracted from the forwarded From: header."""
        write_fixture(tmp_path, CLOUDFLARE_FORWARDED)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        # Body contains "From: Cloudflare <noreply@notify.cloudflare.com>"
        assert tx.description == "Cloudflare"

    def test_invoice_amount_pattern(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CLOUDFLARE_FORWARDED)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.amount == decimal.Decimal("-11.51")

    def test_you_paid_pattern_with_comma(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, TUNE_UP_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.amount == decimal.Decimal("-2025.00")

    def test_payment_amount_of_pattern(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, GOOGLE_PAYMENT_RECEIVED)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.amount == decimal.Decimal("-5.63")

    def test_payment_of_pattern(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, ISSAQUAH_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.amount == decimal.Decimal("-891.07")

    # ------------------------------------------------------------------
    # Amount fallback → needs_review
    # ------------------------------------------------------------------

    def test_no_amount_sets_needs_review_with_reason(
        self, tmp_path: Path, session: Session
    ) -> None:
        """When no amount can be extracted, amount=None and status=needs_review."""
        write_fixture(tmp_path, GOOGLE_WORKSPACE_INVOICE)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value
        assert tx.review_reason is not None
        assert "Amount could not be extracted" in tx.review_reason
        # Amount is NULL (None), not $0.00, so the dashboard shows "Amount missing".
        assert tx.amount is None

    def test_no_plain_text_sets_needs_review(
        self, tmp_path: Path, session: Session
    ) -> None:
        """'No plain text body available' body → amount=None, needs_review."""
        write_fixture(tmp_path, NO_PLAIN_TEXT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value
        assert tx.amount is None

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def test_pdf_attachments_linked(self, tmp_path: Path, session: Session) -> None:
        write_fixture(tmp_path, ANTHROPIC_RECEIPT)
        hex_id = ANTHROPIC_RECEIPT["id"]
        pdf = tmp_path / f"{hex_id}_Receipt-2355-2148.pdf"
        pdf.write_bytes(b"%PDF stub")

        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()

        assert tx.attachments is not None
        attachment_paths = tx.attachments
        assert any("Receipt-2355-2148.pdf" in p for p in attachment_paths)

    def test_json_source_file_included_in_attachments(
        self, tmp_path: Path, session: Session
    ) -> None:
        json_path = write_fixture(tmp_path, RUNPOD_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert any(str(json_path.resolve()) in p for p in tx.attachments)

    def test_no_attachments_still_has_json(
        self, tmp_path: Path, session: Session
    ) -> None:
        json_path = write_fixture(tmp_path, RENDER_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert len(tx.attachments) == 1
        assert tx.attachments[0] == str(json_path.resolve())

    # ------------------------------------------------------------------
    # IngestedFile tracking
    # ------------------------------------------------------------------

    def test_ingested_file_record_created(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, ANTHROPIC_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        ingested = session.query(IngestedFile).one()
        assert ingested.adapter == Source.GMAIL_N8N.value
        assert ingested.status == FileStatus.SUCCESS.value
        assert len(ingested.transaction_ids) == 1

    def test_ingested_file_links_to_transaction(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, RUNPOD_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        ingested = session.query(IngestedFile).one()
        assert ingested.transaction_ids == [tx.id]

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def test_running_twice_does_not_create_duplicate(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, ANTHROPIC_RECEIPT)
        adapter = self._make_adapter(tmp_path)

        result1 = adapter.run(session)
        result2 = adapter.run(session)

        assert result1.records_created == 1
        assert result2.records_created == 0
        assert result2.records_skipped == 1

        assert session.query(Transaction).count() == 1

    def test_duplicate_file_contents_in_second_dir_skipped(
        self, tmp_path: Path, session: Session
    ) -> None:
        """Same file bytes copied to a second directory must not create a duplicate."""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        payload_str = json.dumps([ANTHROPIC_RECEIPT])
        (dir1 / "2025-03-09_Anthropic.json").write_text(payload_str)
        (dir2 / "2025-03-09_Anthropic_copy.json").write_text(payload_str)

        adapter = GmailN8nAdapter(source_dirs=[str(dir1), str(dir2)])
        result = adapter.run(session)

        # One created, one skipped (same file hash)
        assert result.records_created == 1
        assert result.records_skipped == 1
        assert session.query(Transaction).count() == 1

    def test_multiple_different_files_all_ingested(
        self, tmp_path: Path, session: Session
    ) -> None:
        fixtures = [
            ANTHROPIC_RECEIPT,
            RUNPOD_RECEIPT,
            CLOUDFLARE_FORWARDED,
            RENDER_RECEIPT,
        ]
        for fixture in fixtures:
            write_fixture(tmp_path, fixture)

        adapter = self._make_adapter(tmp_path)
        result = adapter.run(session)

        assert result.records_created == 4
        assert result.records_failed == 0
        assert session.query(Transaction).count() == 4

    # ------------------------------------------------------------------
    # Error isolation
    # ------------------------------------------------------------------

    def test_malformed_json_does_not_halt_batch(
        self, tmp_path: Path, session: Session
    ) -> None:
        # Write a bad JSON file
        (tmp_path / "bad.json").write_text("not valid json", encoding="utf-8")
        # Write a valid file
        write_fixture(tmp_path, ANTHROPIC_RECEIPT)

        adapter = self._make_adapter(tmp_path)
        result = adapter.run(session)

        # Valid file still ingested despite bad file
        assert result.records_created == 1
        assert result.records_failed == 1

    def test_missing_id_field_is_counted_as_failure(
        self, tmp_path: Path, session: Session
    ) -> None:
        bad_payload = {k: v for k, v in ANTHROPIC_RECEIPT.items() if k != "id"}
        bad_path = tmp_path / "no_id.json"
        bad_path.write_text(json.dumps([bad_payload]))

        adapter = self._make_adapter(tmp_path)
        result = adapter.run(session)

        assert result.records_failed == 1
        assert session.query(Transaction).count() == 0

    def test_nonexistent_directory_silently_skipped(
        self, session: Session
    ) -> None:
        adapter = GmailN8nAdapter(source_dirs=["/tmp/does_not_exist_xyz_abc"])
        result = adapter.run(session)
        assert result.records_created == 0
        assert result.records_failed == 0

    # ------------------------------------------------------------------
    # Source hash
    # ------------------------------------------------------------------

    def test_source_hash_is_64_char_hex(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, ANTHROPIC_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert len(tx.source_hash) == 64
        assert all(c in "0123456789abcdef" for c in tx.source_hash)

    def test_source_type_is_gmail_n8n(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, ANTHROPIC_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.source == Source.GMAIL_N8N.value


# ---------------------------------------------------------------------------
# REQ-GMOBJ-01: reject `[object Object]` description at ingest
# ---------------------------------------------------------------------------


class TestObjectObjectPayload:
    def _make_adapter(self, *dirs: Path) -> GmailN8nAdapter:
        return GmailN8nAdapter(source_dirs=[str(d) for d in dirs])

    def test_object_object_payload_routes_to_needs_review(
        self, tmp_path: Path, session: Session
    ) -> None:
        """accounting#85: a `[object Object]`-literal vendor must route the
        record to NEEDS_REVIEW with a review reason, not pass through
        silently."""
        write_fixture(tmp_path, OBJECT_OBJECT_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        tx = session.query(Transaction).one()
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value
        assert tx.review_reason is not None
        assert "object object" in tx.review_reason.lower()

    def test_object_object_literal_never_stored_as_description(
        self, tmp_path: Path, session: Session
    ) -> None:
        """The literal string "[object Object]" must never be stored as the
        plain description — it can never match a VendorRule and can never be
        learned."""
        write_fixture(tmp_path, OBJECT_OBJECT_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        tx = session.query(Transaction).one()
        assert tx.description != "[object Object]"

    def test_well_formed_record_unaffected_by_object_object_guard(
        self, tmp_path: Path, session: Session
    ) -> None:
        """The guard must be narrowly scoped: a normal, well-formed vendor
        description is stored and ingested exactly as before."""
        write_fixture(tmp_path, ANTHROPIC_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        tx = session.query(Transaction).one()
        assert tx.description == "Anthropic, PBC"
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value  # classification pending, as today


class TestObjectObjectVendorShapes:
    """REQ-GMOBJ-01: no gmail row ever stores the literal, whatever the shape."""

    def test_bare_literal_from_field(self) -> None:
        assert "[object Object]" not in extract_vendor("[object Object]")

    def test_literal_inside_angle_brackets(self) -> None:
        assert "[object Object]" not in extract_vendor("Acme <[object Object]>")

    def test_literal_in_forwarded_from_line(self) -> None:
        body = (
            "---------- Forwarded message ---------\n"
            "From: [object Object] <billing@mail.elevenlabs.io>\n"
        )
        vendor = extract_vendor("Travis Sparks <sparkst@gmail.com>", body)
        assert "[object Object]" not in vendor
        # review round 2: the fallback must use the *forwarded* sender's
        # domain, not the outer self-forward envelope (sparkst@gmail.com).
        assert vendor == "elevenlabs.io"

    def test_display_name_literal_falls_back_to_domain(self) -> None:
        assert extract_vendor(
            "[object Object] <billing@mail.elevenlabs.io>"
        ) == "elevenlabs.io"


class TestObjectObjectOCRPreservesMarker:
    def test_ocr_branch_appends_not_overwrites_corrupted_marker(
        self, tmp_path: Path, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """review round 2 (P2): a corrupted-payload row whose body is empty but
        whose attachment OCR succeeds must keep the CORRUPTED_PAYLOAD_MARKER in
        review_reason, not have it clobbered by the OCR note."""
        import src.adapters.gmail_n8n as gmail_n8n_mod
        from src.utils.receipt_ocr import OCRResult

        record = dict(OBJECT_OBJECT_RECEIPT)
        record["body_text"] = ""  # forces the body_is_empty OCR branch
        write_fixture(tmp_path, record)
        # Co-locate a fake receipt image so find_extractable_attachments finds one.
        (tmp_path / f"{record['id']}_receipt.png").write_bytes(b"fake")

        monkeypatch.setattr(
            gmail_n8n_mod,
            "extract_receipt",
            lambda _path: OCRResult(
                vendor="Elevenlabs", amount=decimal.Decimal("24.27"), success=True
            ),
        )

        GmailN8nAdapter(source_dirs=[str(tmp_path)]).run(session)

        tx = session.query(Transaction).one()
        assert gmail_n8n_mod.CORRUPTED_PAYLOAD_MARKER in (tx.review_reason or "")
        assert "Auto-extracted from attachment" in (tx.review_reason or "")


class TestObjectObjectSelfForwardIngest:
    def test_self_forwarded_literal_flags_needs_review(
        self, tmp_path: Path, session: Session
    ) -> None:
        """A self-forward whose body From: carries the literal must still be
        flagged, and must not store the literal as the description."""
        record = dict(OBJECT_OBJECT_RECEIPT)
        record["from"] = "Travis Sparks <sparkst@gmail.com>"
        record["body_text"] = (
            "---------- Forwarded message ---------\n"
            "From: [object Object] <billing@mail.elevenlabs.io>\n"
            "Total: $24.27\n"
        )
        write_fixture(tmp_path, record)
        GmailN8nAdapter(source_dirs=[str(tmp_path)]).run(session)

        tx = session.query(Transaction).one()
        assert "[object Object]" not in (tx.description or "")
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value
        assert "object object" in (tx.review_reason or "").lower()


# ---------------------------------------------------------------------------
# Unit tests: forwarded vendor extraction helpers
# ---------------------------------------------------------------------------


class TestExtractForwardedVendor:
    def test_extracts_display_name(self) -> None:
        body = (
            "---------- Forwarded message ---------\n"
            "From: Cloudflare <noreply@notify.cloudflare.com>\n"
            "Invoice Amount: $11.51\n"
        )
        assert _extract_forwarded_vendor(body) == "Cloudflare"

    def test_extracts_apple(self) -> None:
        body = (
            "---------- Forwarded message ---------\n"
            "From: Apple <no_reply@email.apple.com>\n"
        )
        assert _extract_forwarded_vendor(body) == "Apple"

    def test_extracts_wifi_onboard(self) -> None:
        body = (
            "---------- Forwarded message ---------\n"
            "From: Wi-Fi Onboard <info@info.wifionboard.com>\n"
        )
        assert _extract_forwarded_vendor(body) == "Wi-Fi Onboard"

    def test_email_only_uses_domain(self) -> None:
        body = "From: <noreply@dhl.com>\n"
        assert _extract_forwarded_vendor(body) == "DHL"

    def test_unknown_email_returns_domain(self) -> None:
        body = "From: <billing@supplierpayments.com>\n"
        assert _extract_forwarded_vendor(body) == "supplierpayments.com"

    def test_no_forwarded_header_returns_none(self) -> None:
        assert _extract_forwarded_vendor("No forwarded content here.") is None

    def test_empty_body_returns_none(self) -> None:
        assert _extract_forwarded_vendor("") is None


class TestIsSelfForwarded:
    def test_sparkst_gmail(self) -> None:
        assert _is_self_forwarded("Travis Sparks <sparkst@gmail.com>") is True

    def test_sparkry_email(self) -> None:
        assert _is_self_forwarded("Travis Sparks <travis@sparkry.com>") is True

    def test_blacklinemtb_email(self) -> None:
        assert _is_self_forwarded("Travis Sparks <sparkst@blacklinemtb.com>") is True

    def test_travis_sparks_display_name(self) -> None:
        assert _is_self_forwarded("Travis Sparks <other@example.com>") is True

    def test_external_vendor_not_self(self) -> None:
        assert _is_self_forwarded("Cloudflare <noreply@cloudflare.com>") is False

    def test_anthropic_not_self(self) -> None:
        assert _is_self_forwarded("Anthropic, PBC <invoice@mail.anthropic.com>") is False


# ---------------------------------------------------------------------------
# Integration tests: forwarded emails and None amounts
# ---------------------------------------------------------------------------


class TestForwardedEmailIntegration:
    def _make_adapter(self, *dirs: Path) -> GmailN8nAdapter:
        return GmailN8nAdapter(source_dirs=[str(d) for d in dirs])

    def test_forwarded_wifi_vendor_extracted(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, FORWARDED_WIFI_ONBOARD)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.description == "Wi-Fi Onboard"

    def test_forwarded_apple_vendor_extracted(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, FORWARDED_APPLE_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.description == "Apple"

    def test_forwarded_apple_amount_extracted(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, FORWARDED_APPLE_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.amount == decimal.Decimal("-2.99")

    def test_wifi_onboard_no_amount_stored_as_none(
        self, tmp_path: Path, session: Session
    ) -> None:
        """Wi-Fi Onboard bodies with no dollar amount → amount=None."""
        write_fixture(tmp_path, WIFI_ONBOARD_DIRECT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.amount is None
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value


# ---------------------------------------------------------------------------
# Tests — REQ-FIX-ING-002: Decimal-only exchange-rate math
# ---------------------------------------------------------------------------

USD_AND_FOREIGN_RECEIPT: dict[str, object] = {
    "id": "aa11bb22cc33dd44",
    "filename": "2025-05-01_Foreign_Vendor_aa11bb22cc33dd44",
    "date": "2025-05-01T12:00:00.000Z",
    "from": "Foreign Vendor <billing@foreignvendor.example>",
    "subject": "Your receipt from Foreign Vendor",
    "body_text": (
        "Receipt from Foreign Vendor\n"
        "Amount paid $10.00\n"
        "Local charge: £8.00\n"
    ),
    "body_html": "<html></html>",
}


class TestForeignCurrencyDecimalMath:
    """REQ-FIX-ING-002: a receipt carrying BOTH a USD amount and a detected
    foreign amount must ingest without a float/Decimal TypeError, and the
    stored exchange_rate must be a Decimal."""

    def test_usd_and_foreign_amount_ingests_without_typeerror(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, USD_AND_FOREIGN_RECEIPT)
        adapter = GmailN8nAdapter(source_dirs=[str(tmp_path)])
        result = adapter.run(session)

        assert result.records_created == 1
        assert result.records_failed == 0

        tx = session.query(Transaction).one()
        assert tx.amount == decimal.Decimal("-10.00")
        assert tx.currency_code == "GBP"
        assert tx.amount_foreign == decimal.Decimal("8.00")
        assert tx.exchange_rate is not None
        assert isinstance(tx.exchange_rate, decimal.Decimal)
        # 10.00 / 8.00 = 1.25, quantized to 8dp.
        assert tx.exchange_rate == decimal.Decimal("1.25000000")
        assert tx.exchange_rate_source == "email_extracted"


# ---------------------------------------------------------------------------
# Tests — REQ-FIX-ING-003: per-file rollback on failure
# ---------------------------------------------------------------------------


class TestPerFileRollback:
    """REQ-FIX-ING-003: a mid-file exception rolls back only that file's
    partial rows; the session remains usable for subsequent files."""

    def test_middle_file_failure_does_not_poison_batch(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, ANTHROPIC_RECEIPT)
        write_fixture(tmp_path, RUNPOD_RECEIPT)
        write_fixture(tmp_path, TUNE_UP_RECEIPT)

        import src.adapters.gmail_n8n as gmail_mod

        orig_record = gmail_mod.GmailN8nAdapter._record_ingested_file
        state = {"count": 0}

        def flaky_record_ingested_file(
            session: Session,
            json_path: Path,
            file_hash: str,
            status: FileStatus,
            transaction_ids: list[str],
        ) -> None:
            state["count"] += 1
            if state["count"] == 2:
                raise RuntimeError("forced failure on file 2")
            orig_record(session, json_path, file_hash, status, transaction_ids)

        monkey_target = gmail_mod.GmailN8nAdapter._record_ingested_file
        gmail_mod.GmailN8nAdapter._record_ingested_file = staticmethod(  # type: ignore[method-assign]
            flaky_record_ingested_file
        )
        try:
            adapter = GmailN8nAdapter(source_dirs=[str(tmp_path)])
            result = adapter.run(session)
        finally:
            gmail_mod.GmailN8nAdapter._record_ingested_file = monkey_target  # type: ignore[method-assign]

        assert result.records_created == 2
        assert result.records_failed == 1

        # The session must still be usable: 2 committed transactions, and no
        # partial/orphaned row from the failed file. Files are processed in
        # sorted-glob order (2025-03-09 Anthropic, 2025-04-12 RunPod,
        # 2025-06-18 Tune-Up) — the 2nd file (RunPod) is the forced failure.
        transactions = session.query(Transaction).all()
        assert len(transactions) == 2
        descriptions = {tx.description for tx in transactions}
        assert descriptions == {"Anthropic, PBC", "Travis Sparks"}

        # No partial IngestedFile row for the failed file either.
        ingested_files = session.query(IngestedFile).all()
        assert len(ingested_files) == 2
