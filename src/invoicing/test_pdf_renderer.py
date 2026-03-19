"""Tests for src/invoicing/pdf_renderer.py.

REQ-INV-004: PDF Export — Sparkry Template

Tests verify:
- PDF bytes start with %PDF (valid PDF file)
- HTML output contains key elements (header bar, address, totals, etc.)
- Section header grouping appears for hourly invoices
- No section header for flat-rate invoices
- Grand total is correct
- Multi-page / multi-item invoices still produce valid PDF
- Late fee notice is present when late_fee_pct > 0
- Adjustments row is present when non-zero
- Notes appear in HTML and PDF
- Timeout parameter is wired (smoke test only — not a real 30s wait)
"""

from __future__ import annotations

import decimal
import types

from src.invoicing.pdf_renderer import render_html, render_pdf

# ---------------------------------------------------------------------------
# Test fixtures (simple namespace objects — no DB required)
# ---------------------------------------------------------------------------


def _make_invoice(
    *,
    invoice_number: str = "202503-001",
    project: str | None = "Fascinate OS",
    submitted_date: str | None = "2026-03-01",
    due_date: str | None = "2026-03-15",
    subtotal: str = "1000.00",
    adjustments: str = "0.00",
    total: str = "1000.00",
    notes: str | None = "Introductory Rate: $100/hr",
    late_fee_pct: float = 0.10,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        invoice_number=invoice_number,
        project=project,
        submitted_date=submitted_date,
        due_date=due_date,
        subtotal=decimal.Decimal(subtotal),
        adjustments=decimal.Decimal(adjustments),
        total=decimal.Decimal(total),
        notes=notes,
        late_fee_pct=late_fee_pct,
    )


def _make_item(
    *,
    description: str = "Jan 5 — Fascinate OS sync",
    quantity: str = "1",
    unit_price: str = "100.00",
    total_price: str = "100.00",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        description=description,
        quantity=decimal.Decimal(quantity),
        unit_price=decimal.Decimal(unit_price),
        total_price=decimal.Decimal(total_price),
    )


def _make_customer(
    *,
    name: str = "How To Fascinate",
    billing_model: str = "hourly",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, billing_model=billing_model)


# ---------------------------------------------------------------------------
# PDF smoke tests — REQ-INV-004
# ---------------------------------------------------------------------------


class TestRenderPdfValid:
    """PDF bytes must be a valid PDF document."""

    def test_pdf_starts_with_pdf_magic(self) -> None:
        """REQ-INV-004: PDF output starts with %PDF magic bytes."""
        inv = _make_invoice()
        items = [_make_item()]
        cust = _make_customer()

        pdf_bytes = render_pdf(inv, items, cust)

        assert isinstance(pdf_bytes, bytes)
        assert pdf_bytes[:4] == b"%PDF", (
            f"Expected PDF to start with %PDF, got {pdf_bytes[:8]!r}"
        )

    def test_pdf_nonempty(self) -> None:
        inv = _make_invoice()
        items = [_make_item()]
        cust = _make_customer()

        pdf_bytes = render_pdf(inv, items, cust)

        assert len(pdf_bytes) > 1024, "PDF appears too small to be valid"

    def test_pdf_with_no_line_items(self) -> None:
        """Empty line-item list should still produce a valid PDF."""
        inv = _make_invoice(subtotal="0.00", total="0.00")
        pdf_bytes = render_pdf(inv, [], _make_customer())
        assert pdf_bytes[:4] == b"%PDF"

    def test_pdf_multi_item_hourly(self) -> None:
        """Multiple line items for hourly billing — still valid PDF."""
        inv = _make_invoice(subtotal="1000.00", total="1000.00")
        items = [
            _make_item(description=f"Session {i}", quantity="1")
            for i in range(10)
        ]
        pdf_bytes = render_pdf(inv, items, _make_customer())
        assert pdf_bytes[:4] == b"%PDF"

    def test_pdf_flat_rate_invoice(self) -> None:
        """Cardinal Health flat-rate invoice produces valid PDF."""
        inv = _make_invoice(
            invoice_number="CH20260228",
            project="AI Product Engineering Coaching",
            submitted_date="2026-03-02",
            due_date="2026-05-31",
            subtotal="33000.00",
            total="33000.00",
            notes=None,
            late_fee_pct=0.0,
        )
        items = [
            _make_item(
                description="AI Product Engineering Coaching Month 2",
                quantity="1",
                unit_price="33000.00",
                total_price="33000.00",
            )
        ]
        cust = _make_customer(name="Cardinal Health, Inc.", billing_model="flat_rate")
        pdf_bytes = render_pdf(inv, items, cust)
        assert pdf_bytes[:4] == b"%PDF"

    def test_pdf_with_adjustments(self) -> None:
        """Non-zero adjustments produce a valid PDF."""
        inv = _make_invoice(
            subtotal="1000.00",
            adjustments="-100.00",
            total="900.00",
        )
        pdf_bytes = render_pdf(inv, [_make_item()], _make_customer())
        assert pdf_bytes[:4] == b"%PDF"

    def test_pdf_ends_with_eof_marker(self) -> None:
        """PDF should end with %%EOF."""
        inv = _make_invoice()
        pdf_bytes = render_pdf(inv, [_make_item()], _make_customer())
        # fpdf2 may append trailing whitespace/newlines
        assert b"%%EOF" in pdf_bytes[-64:], "PDF missing %%EOF marker"


# ---------------------------------------------------------------------------
# HTML smoke tests — REQ-INV-004 (browser-print fallback)
# ---------------------------------------------------------------------------


class TestRenderHtmlContent:
    """HTML output must contain all required visual elements."""

    def _html(
        self,
        *,
        billing_model: str = "hourly",
        late_fee_pct: float = 0.10,
        adjustments: str = "0.00",
        notes: str | None = "Introductory Rate: $100/hr",
    ) -> str:
        inv = _make_invoice(
            late_fee_pct=late_fee_pct,
            adjustments=adjustments,
            notes=notes,
        )
        items = [_make_item()]
        cust = _make_customer(billing_model=billing_model)
        return render_html(inv, items, cust)

    def test_html_returns_string(self) -> None:
        assert isinstance(self._html(), str)

    def test_html_orange_header_bar(self) -> None:
        """REQ-INV-004: Orange header bar colour present in HTML."""
        html = self._html()
        assert "#F97316" in html or "F97316" in html, (
            "Orange header colour #F97316 missing from HTML"
        )

    def test_html_sparkry_address(self) -> None:
        """REQ-INV-004: Sparkry LLC address block in HTML."""
        html = self._html()
        assert "Sparkry LLC" in html
        assert "24517 SE 43rd Pl" in html
        assert "Sammamish, WA 98029" in html
        assert "(919) 491-3894" in html

    def test_html_invoice_title(self) -> None:
        html = self._html()
        assert "Invoice" in html

    def test_html_customer_name(self) -> None:
        html = self._html()
        assert "How To Fascinate" in html

    def test_html_invoice_number(self) -> None:
        html = self._html()
        assert "202503-001" in html

    def test_html_submitted_date(self) -> None:
        html = self._html()
        assert "03/01/2026" in html

    def test_html_due_date(self) -> None:
        html = self._html()
        assert "03/15/2026" in html

    def test_html_payable_to_sparkry(self) -> None:
        html = self._html()
        assert "Payable to" in html

    def test_html_column_headers(self) -> None:
        """Line items table must have Description, Qty, Unit price, Total price."""
        html = self._html()
        assert "Description" in html
        assert "Qty" in html
        assert "Unit price" in html
        assert "Total price" in html

    def test_html_section_header_for_hourly(self) -> None:
        """REQ-INV-004: Section header grouping row for hourly billing."""
        html = self._html(billing_model="hourly")
        # Section header like "Fascinate OS Project - 1 Hour"
        assert "Fascinate OS" in html
        assert "Hour" in html

    def test_html_no_section_header_for_flat_rate(self) -> None:
        """Flat-rate invoices must not produce an hourly grouping header row."""
        inv = _make_invoice(project="AI Coaching")
        items = [
            _make_item(
                description="AI Coaching Month 1",
                quantity="1",
                unit_price="33000.00",
                total_price="33000.00",
            )
        ]
        cust = _make_customer(billing_model="flat_rate")
        html = render_html(inv, items, cust)
        # No "<tr class=\"section-header\">" row should appear in the rendered body.
        # The CSS rule exists in <style> but the element must not be rendered.
        assert '<tr class="section-header">' not in html

    def test_html_subtotal(self) -> None:
        html = self._html()
        assert "1,000.00" in html

    def test_html_grand_total_pink(self) -> None:
        """REQ-INV-004: Grand total displayed in magenta/pink (#EC4899)."""
        html = self._html()
        assert "#EC4899" in html or "EC4899" in html, (
            "Pink total colour #EC4899 missing from HTML"
        )

    def test_html_late_fee_notice_present(self) -> None:
        """REQ-INV-004: Late fee notice when late_fee_pct > 0."""
        html = self._html(late_fee_pct=0.10)
        assert "10% Late Fee" in html
        assert "due date" in html

    def test_html_no_late_fee_notice_when_zero(self) -> None:
        html = self._html(late_fee_pct=0.0)
        assert "Late Fee" not in html

    def test_html_adjustments_row_when_nonzero(self) -> None:
        html = self._html(adjustments="-100.00")
        assert "Adjustments" in html

    def test_html_no_adjustments_row_when_zero(self) -> None:
        html = self._html(adjustments="0.00")
        # The label "Adjustments" should be absent
        assert "Adjustments" not in html

    def test_html_notes_displayed(self) -> None:
        html = self._html(notes="Introductory Rate: $100/hr")
        assert "Introductory Rate" in html

    def test_html_no_notes_block_when_none(self) -> None:
        html = self._html(notes=None)
        # "Notes" label must not appear — the notes-label div is conditional.
        # (The CSS class "notes-block" will still appear in the <style> section.)
        assert '<div class="notes-label">' not in html

    def test_html_is_valid_html_doctype(self) -> None:
        html = self._html()
        assert html.strip().startswith("<!DOCTYPE html")

    def test_html_project_name(self) -> None:
        html = self._html()
        assert "Fascinate OS" in html


# ---------------------------------------------------------------------------
# Timeout smoke test
# ---------------------------------------------------------------------------


class TestRenderPdfTimeout:
    def test_timeout_parameter_accepted(self) -> None:
        """render_pdf accepts a custom timeout without raising for fast renders."""
        inv = _make_invoice()
        items = [_make_item()]
        cust = _make_customer()
        # Should complete well within 10 seconds for a single-page invoice
        pdf_bytes = render_pdf(inv, items, cust, timeout=10.0)
        assert pdf_bytes[:4] == b"%PDF"
