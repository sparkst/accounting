"""PDF and HTML invoice renderer for Sparkry LLC.

Public API
----------
render_pdf(invoice, line_items, customer) -> bytes
    Returns a PDF document as bytes using fpdf2 (pure Python, zero system deps).
    Raises TimeoutError if generation exceeds 30 seconds.

render_html(invoice, line_items, customer) -> str
    Returns an HTML string rendered from the Jinja2 template at
    src/invoicing/templates/invoice.html.  Useful as a browser-print fallback.

Design notes
------------
- All amounts formatted as strings with comma thousands separators ("1,000.00").
- Line items are grouped by a synthetic section header derived from the
  invoice project name and total quantity when billing_model is hourly.
  Cardinal Health flat-rate invoices have no section header grouping row.
- Multi-page PDFs repeat the orange header bar on every page and print page
  numbers ("Page N of M") at the bottom of each page.
- The orange brand colour is #F97316; the grand-total colour is #EC4899.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import decimal
from collections.abc import Sequence
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional Jinja2 import — only required for render_html
# ---------------------------------------------------------------------------
try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    _JINJA2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JINJA2_AVAILABLE = False

from fpdf import FPDF  # type: ignore[import-untyped]
from fpdf.enums import XPos, YPos  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Colour palette (RGB tuples)
# ---------------------------------------------------------------------------
_ORANGE: tuple[int, int, int] = (249, 115, 22)   # #F97316
_PINK: tuple[int, int, int] = (236, 72, 153)      # #EC4899
_BLACK: tuple[int, int, int] = (17, 24, 39)       # near-black
_DARK_GREY: tuple[int, int, int] = (55, 65, 81)
_MID_GREY: tuple[int, int, int] = (107, 114, 128)
_LIGHT_GREY: tuple[int, int, int] = (229, 231, 235)
_SECTION_BG: tuple[int, int, int] = (249, 250, 251)
_WHITE: tuple[int, int, int] = (255, 255, 255)

_LOGO_PATH = Path(__file__).parent / "assets" / "sparkry-logo.png"

# ---------------------------------------------------------------------------
# Page geometry
# ---------------------------------------------------------------------------
_PAGE_W = 210  # mm  (A4)
_PAGE_H = 297  # mm
_MARGIN_L = 18
_MARGIN_R = 18
_MARGIN_T = 0   # we paint the header bar ourselves
_MARGIN_B = 16
_CONTENT_W = _PAGE_W - _MARGIN_L - _MARGIN_R  # 174 mm

# Column widths for the line-items table (must sum to _CONTENT_W)
_COL_DESC = 90
_COL_QTY = 18
_COL_UNIT = 30
_COL_TOTAL = 36

# Row heights
_ROW_H = 7      # regular line item
_HEADER_ROW_H = 8  # table column headers
_SECTION_ROW_H = 8  # section header row

# ---------------------------------------------------------------------------
# Internal data transfer objects (not SQLAlchemy models — avoids DB deps)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _LineItemDTO:
    description: str
    quantity: decimal.Decimal
    unit_price: decimal.Decimal
    total_price: decimal.Decimal


@dataclasses.dataclass
class _InvoiceDTO:
    invoice_number: str
    project: str | None
    submitted_date: str | None   # YYYY-MM-DD
    due_date: str | None         # YYYY-MM-DD
    subtotal: decimal.Decimal
    adjustments: decimal.Decimal
    total: decimal.Decimal
    notes: str | None
    late_fee_pct: float


@dataclasses.dataclass
class _CustomerDTO:
    name: str
    billing_model: str  # "hourly" | "flat_rate" | "project"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_amount(value: decimal.Decimal | float | None) -> str:
    """Format a decimal as "1,234.56" (no currency symbol)."""
    if value is None:
        return "0.00"
    d = decimal.Decimal(str(value))
    return f"{d:,.2f}"


def _fmt_qty(value: decimal.Decimal | float) -> str:
    """Format quantity: drop trailing zeros after decimal point."""
    d = decimal.Decimal(str(value)).normalize()
    # Show at most 2 decimal places
    rounded = d.quantize(decimal.Decimal("0.01")).normalize()
    return str(rounded)


def _parse_date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


_LATIN1_SUBSTITUTIONS: dict[str, str] = {
    "\u2014": "-",   # em dash  —
    "\u2013": "-",   # en dash  –
    "\u2018": "'",   # left single quotation mark
    "\u2019": "'",   # right single quotation mark
    "\u201c": '"',   # left double quotation mark
    "\u201d": '"',   # right double quotation mark
    "\u2026": "...", # ellipsis
    "\u00a0": " ",   # non-breaking space
}


def _pdf_text(text: str) -> str:
    """Replace Unicode characters unsupported by the built-in Helvetica font."""
    for char, replacement in _LATIN1_SUBSTITUTIONS.items():
        text = text.replace(char, replacement)
    # Final safety: drop anything still outside latin-1 range
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _fmt_date(iso: str | None) -> str:
    d = _parse_date(iso)
    if d is None:
        return "—"
    return d.strftime("%m/%d/%Y")


def _total_hours(items: Sequence) -> decimal.Decimal:
    return sum(
        (decimal.Decimal(str(it.quantity)) for it in items),
        decimal.Decimal("0"),
    )


def _build_dtos(invoice: object, line_items: Sequence, customer: object) -> tuple[
    _InvoiceDTO, list[_LineItemDTO], _CustomerDTO
]:
    """Extract only the fields we need into simple dataclasses."""
    inv = _InvoiceDTO(
        invoice_number=str(getattr(invoice, "invoice_number", "")),
        project=getattr(invoice, "project", None),
        submitted_date=getattr(invoice, "submitted_date", None),
        due_date=getattr(invoice, "due_date", None),
        subtotal=decimal.Decimal(str(getattr(invoice, "subtotal", "0"))),
        adjustments=decimal.Decimal(str(getattr(invoice, "adjustments", "0"))),
        total=decimal.Decimal(str(getattr(invoice, "total", "0"))),
        notes=getattr(invoice, "notes", None),
        late_fee_pct=float(getattr(invoice, "late_fee_pct", 0.0)),
    )
    items = [
        _LineItemDTO(
            description=str(getattr(it, "description", "")),
            quantity=decimal.Decimal(str(getattr(it, "quantity", "1"))),
            unit_price=decimal.Decimal(str(getattr(it, "unit_price", "0"))),
            total_price=decimal.Decimal(str(getattr(it, "total_price", "0"))),
        )
        for it in line_items
    ]
    cust = _CustomerDTO(
        name=str(getattr(customer, "name", "")),
        billing_model=str(getattr(customer, "billing_model", "project")),
    )
    return inv, items, cust


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def render_html(invoice: object, line_items: Sequence, customer: object) -> str:
    """Render the invoice as an HTML string using Jinja2.

    Parameters
    ----------
    invoice:    Invoice ORM instance (or any object with matching attributes).
    line_items: Sequence of InvoiceLineItem ORM instances (or duck-typed objects).
    customer:   Customer ORM instance (or duck-typed object).

    Returns
    -------
    str: Fully rendered HTML document.

    Raises
    ------
    ImportError: If jinja2 is not installed.
    """
    if not _JINJA2_AVAILABLE:  # pragma: no cover
        raise ImportError("jinja2 is required for render_html — pip install jinja2")

    inv, items, cust = _build_dtos(invoice, line_items, customer)

    # Build grouped line items
    groups = _build_groups(inv, items, cust)

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("invoice.html")

    late_fee_notice = ""
    if inv.late_fee_pct and inv.late_fee_pct > 0:
        pct_int = int(round(inv.late_fee_pct * 100))
        late_fee_notice = f"* {pct_int}% Late Fee if paid after due date."

    return tmpl.render(
        invoice=inv,
        customer=cust,
        groups=groups,
        submitted_date_fmt=_fmt_date(inv.submitted_date),
        due_date_fmt=_fmt_date(inv.due_date),
        subtotal_fmt=_fmt_amount(inv.subtotal),
        adjustments_fmt=_fmt_amount(inv.adjustments),
        adjustments_nonzero=inv.adjustments != decimal.Decimal("0"),
        total_fmt=_fmt_amount(inv.total),
        late_fee_notice=late_fee_notice,
    )


# ---------------------------------------------------------------------------
# Section grouping logic (shared by both renderers)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Group:
    header: str | None
    items: list[_LineItemDTO]


def _build_groups(
    inv: _InvoiceDTO,
    items: list[_LineItemDTO],
    cust: _CustomerDTO,
) -> list[_Group]:
    """Group line items under a section header for hourly invoices.

    For hourly billing: one group with a header like
    "Fascinate OS Project - 10 Hours".

    For flat-rate/project billing: no section header, one group.
    """
    if not items:
        return [_Group(header=None, items=[])]

    if cust.billing_model == "hourly":
        total_qty = _total_hours(items)
        qty_str = _fmt_qty(total_qty)
        unit_label = "Hour" if total_qty == decimal.Decimal("1") else "Hours"
        project_label = inv.project or "Project"
        header = f"{project_label} - {qty_str} {unit_label}"
        return [_Group(header=header, items=list(items))]

    # flat_rate / project — no grouping header
    return [_Group(header=None, items=list(items))]


# ---------------------------------------------------------------------------
# FPDF subclass with page header / footer
# ---------------------------------------------------------------------------


class _InvoicePDF(FPDF):
    def __init__(self, inv: _InvoiceDTO, cust: _CustomerDTO) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self._inv = inv
        self._cust = cust
        self._total_pages = 1  # updated before output

    # ------------------------------------------------------------------
    # fpdf2 hook: called at the start of every page
    # ------------------------------------------------------------------
    def header(self) -> None:
        # Orange bar — 8 mm tall, full page width
        self.set_fill_color(*_ORANGE)
        self.rect(0, 0, _PAGE_W, 8, style="F")

        # Logo (1738x762 = 2.28:1 ratio, rendered at ~40mm wide)
        logo_w, logo_h = 40, 17.5
        self.image(str(_LOGO_PATH), x=_MARGIN_L, y=12, w=logo_w, h=logo_h)

        # Address block starts below logo
        addr_y = 12 + logo_h + 2
        self.set_font("Helvetica", size=9)
        self.set_text_color(*_DARK_GREY)
        self.set_xy(_MARGIN_L, addr_y)
        self.cell(0, 5, "24517 SE 43rd Pl", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_x(_MARGIN_L)
        self.cell(0, 5, "Sammamish, WA 98029", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_x(_MARGIN_L)
        self.cell(0, 5, "(919) 491-3894", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ------------------------------------------------------------------
    # fpdf2 hook: called at the end of every page
    # ------------------------------------------------------------------
    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", size=8)
        self.set_text_color(*_MID_GREY)
        page_text = f"Page {self.page_no()} of {{nb}}"
        self.cell(0, 6, page_text, align="C")


# ---------------------------------------------------------------------------
# PDF renderer
# ---------------------------------------------------------------------------


def _do_render_pdf(
    inv: _InvoiceDTO,
    items: list[_LineItemDTO],
    cust: _CustomerDTO,
) -> bytes:
    """Core PDF rendering — runs inside a thread so we can enforce a timeout."""
    pdf = _InvoicePDF(inv, cust)
    pdf.alias_nb_pages()  # enables {nb} total-page substitution
    pdf.set_auto_page_break(auto=True, margin=_MARGIN_B + 6)
    pdf.add_page()

    # ── "Invoice" title  ────────────────────────────────────────────────────
    pdf.set_xy(_MARGIN_L, 50)
    pdf.set_font("Helvetica", style="B", size=22)
    pdf.set_text_color(*_BLACK)
    pdf.cell(0, 10, "Invoice", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ── Submitted on  ───────────────────────────────────────────────────────
    pdf.set_x(_MARGIN_L)
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(*_MID_GREY)
    submitted_text = f"Submitted on:  {_fmt_date(inv.submitted_date)}"
    pdf.cell(0, 6, _pdf_text(submitted_text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ── Meta grid  ──────────────────────────────────────────────────────────
    col_w = _CONTENT_W / 3

    def _meta_cell(label: str, value: str, x: float, y: float) -> None:
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", style="B", size=7)
        pdf.set_text_color(*_MID_GREY)
        pdf.cell(col_w, 4, _pdf_text(label.upper()))
        pdf.set_xy(x, y + 4)
        pdf.set_font("Helvetica", size=10)
        pdf.set_text_color(*_BLACK)
        pdf.cell(col_w, 5, _pdf_text(value))

    row1_y = pdf.get_y() + 5
    _meta_cell("Invoice for", cust.name, _MARGIN_L, row1_y)
    _meta_cell("Payable to", "Sparkry LLC", _MARGIN_L + col_w, row1_y)
    _meta_cell("Invoice #", inv.invoice_number, _MARGIN_L + 2 * col_w, row1_y)

    row2_y = row1_y + 13
    _meta_cell("Project", inv.project or "—", _MARGIN_L + col_w, row2_y)
    _meta_cell("Due date", _fmt_date(inv.due_date), _MARGIN_L + 2 * col_w, row2_y)

    # ── Horizontal rule  ────────────────────────────────────────────────────
    divider_y = row2_y + 14
    pdf.set_draw_color(*_LIGHT_GREY)
    pdf.set_line_width(0.3)
    pdf.line(_MARGIN_L, divider_y, _PAGE_W - _MARGIN_R, divider_y)

    # ── Table column headers  ───────────────────────────────────────────────
    th_y = divider_y + 3

    def _th(text: str, x: float, w: float, align: str = "L") -> None:
        pdf.set_xy(x, th_y)
        pdf.set_font("Helvetica", style="B", size=7)
        pdf.set_text_color(*_MID_GREY)
        pdf.cell(w, _HEADER_ROW_H, text.upper(), align=align)  # type: ignore[arg-type]

    _th("Description", _MARGIN_L, _COL_DESC)
    _th("Qty", _MARGIN_L + _COL_DESC, _COL_QTY, "R")
    _th("Unit price", _MARGIN_L + _COL_DESC + _COL_QTY, _COL_UNIT, "R")
    _th("Total price", _MARGIN_L + _COL_DESC + _COL_QTY + _COL_UNIT, _COL_TOTAL, "R")

    # Thin rule under column headers
    rule_y = th_y + _HEADER_ROW_H
    pdf.set_draw_color(*_LIGHT_GREY)
    pdf.line(_MARGIN_L, rule_y, _PAGE_W - _MARGIN_R, rule_y)

    pdf.set_y(rule_y)

    # ── Line item rows  ─────────────────────────────────────────────────────
    groups = _build_groups(inv, items, cust)

    for group in groups:
        # Section header row
        if group.header:
            _draw_section_header(pdf, group.header)

        for item in group.items:
            _draw_line_item(pdf, item)

    # Thin rule after last row
    pdf.set_draw_color(*_LIGHT_GREY)
    after_items_y = pdf.get_y()
    pdf.line(_MARGIN_L, after_items_y, _PAGE_W - _MARGIN_R, after_items_y)

    # ── Totals + Notes  ─────────────────────────────────────────────────────
    totals_y = after_items_y + 6

    # Notes (left side)
    if inv.notes:
        pdf.set_xy(_MARGIN_L, totals_y)
        pdf.set_font("Helvetica", style="B", size=9)
        pdf.set_text_color(*_DARK_GREY)
        pdf.cell(0, 5, "Notes", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_x(_MARGIN_L)
        pdf.set_font("Helvetica", size=9)
        pdf.set_text_color(*_MID_GREY)
        # Multi-line notes support
        pdf.multi_cell(w=_COL_DESC + 10, h=5, text=_pdf_text(inv.notes))

    # Totals (right side) — positioned relative to right margin
    totals_x = _PAGE_W - _MARGIN_R - _COL_TOTAL - _COL_UNIT - 4
    label_w = _COL_UNIT + 4
    amount_w = _COL_TOTAL

    def _totals_row(label: str, amount: str, y: float) -> None:
        pdf.set_xy(totals_x, y)
        pdf.set_font("Helvetica", size=9)
        pdf.set_text_color(*_MID_GREY)
        pdf.cell(label_w, 5, label, align="L")  # type: ignore[arg-type]
        pdf.set_font("Helvetica", size=9)
        pdf.set_text_color(*_BLACK)
        pdf.cell(amount_w, 5, f"${amount}", align="R")  # type: ignore[arg-type]

    _totals_row("Subtotal", _fmt_amount(inv.subtotal), totals_y)

    adj_offset = 0.0
    if inv.adjustments != decimal.Decimal("0"):
        adj_y = totals_y + 6
        _totals_row("Adjustments", _fmt_amount(inv.adjustments), adj_y)
        adj_offset = 6.0

    # Rule before grand total
    rule2_y = totals_y + 6 + adj_offset
    pdf.set_draw_color(*_BLACK)
    pdf.set_line_width(0.4)
    pdf.line(totals_x, rule2_y, _PAGE_W - _MARGIN_R, rule2_y)

    # Grand total — large, pink
    grand_y = rule2_y + 3
    pdf.set_xy(totals_x, grand_y)
    pdf.set_font("Helvetica", style="B", size=18)
    pdf.set_text_color(*_PINK)
    pdf.cell(label_w + amount_w, 10, f"${_fmt_amount(inv.total)}", align="R")  # type: ignore[arg-type]

    # ── Late fee notice  ────────────────────────────────────────────────────
    if inv.late_fee_pct and inv.late_fee_pct > 0:
        pct_int = int(round(inv.late_fee_pct * 100))
        notice = f"* {pct_int}% Late Fee if paid after due date."
        notice_y = grand_y + 16
        pdf.set_xy(_MARGIN_L, notice_y)
        pdf.set_font("Helvetica", size=8)
        pdf.set_text_color(*_MID_GREY)
        pdf.cell(0, 5, notice)

    return bytes(pdf.output())


def _draw_section_header(pdf: _InvoicePDF, text: str) -> None:
    """Draw a bold section-header spanning row."""
    y = pdf.get_y()
    # Light background fill
    pdf.set_fill_color(*_SECTION_BG)
    pdf.rect(_MARGIN_L, y, _CONTENT_W, _SECTION_ROW_H, style="F")
    # Top rule
    pdf.set_draw_color(*_LIGHT_GREY)
    pdf.set_line_width(0.2)
    pdf.line(_MARGIN_L, y, _PAGE_W - _MARGIN_R, y)

    pdf.set_xy(_MARGIN_L + 2, y + 1)
    pdf.set_font("Helvetica", style="B", size=10)
    pdf.set_text_color(*_BLACK)
    pdf.cell(_CONTENT_W, _SECTION_ROW_H - 2, _pdf_text(text))
    pdf.ln(_SECTION_ROW_H)


def _draw_line_item(pdf: _InvoicePDF, item: _LineItemDTO) -> None:
    """Draw a single regular line-item row."""
    y = pdf.get_y()

    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(*_DARK_GREY)

    # Description — indented 4 mm
    pdf.set_xy(_MARGIN_L + 4, y)
    pdf.cell(_COL_DESC - 4, _ROW_H, _pdf_text(item.description), align="L")  # type: ignore[arg-type]

    # Qty
    pdf.set_xy(_MARGIN_L + _COL_DESC, y)
    pdf.cell(_COL_QTY, _ROW_H, _fmt_qty(item.quantity), align="R")  # type: ignore[arg-type]

    # Unit price
    pdf.set_xy(_MARGIN_L + _COL_DESC + _COL_QTY, y)
    pdf.cell(_COL_UNIT, _ROW_H, f"${_fmt_amount(item.unit_price)}", align="R")  # type: ignore[arg-type]

    # Total price
    pdf.set_xy(_MARGIN_L + _COL_DESC + _COL_QTY + _COL_UNIT, y)
    pdf.cell(_COL_TOTAL, _ROW_H, f"${_fmt_amount(item.total_price)}", align="R")  # type: ignore[arg-type]

    # Bottom rule (light)
    pdf.set_draw_color(*_LIGHT_GREY)
    pdf.set_line_width(0.15)
    pdf.line(_MARGIN_L, y + _ROW_H, _PAGE_W - _MARGIN_R, y + _ROW_H)

    pdf.ln(_ROW_H)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_pdf(
    invoice: object,
    line_items: Sequence,
    customer: object,
    timeout: float = 30.0,
) -> bytes:
    """Render a Sparkry LLC invoice as a PDF document.

    Parameters
    ----------
    invoice:    Invoice ORM instance (or any object with matching attributes).
    line_items: Sequence of InvoiceLineItem ORM instances (or duck-typed objects).
    customer:   Customer ORM instance (or duck-typed object).
    timeout:    Maximum seconds to wait for PDF generation (default 30).

    Returns
    -------
    bytes: Raw PDF bytes (starts with b'%PDF').

    Raises
    ------
    TimeoutError: If PDF generation exceeds *timeout* seconds.
    """
    inv, items, cust = _build_dtos(invoice, line_items, customer)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_do_render_pdf, inv, items, cust)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(
                f"PDF generation exceeded {timeout:.0f} second timeout"
            ) from None
