"""CloseReport assembly + dashboard evidence links (REQ-MCA-001, spec §1.4).

Pulls the deterministic reconcile (§1.2) and anomaly (§1.3) results together
with header KPIs, an auto-confirm month summary (§2), and the static
REQ-FIX-DAT-002 data-hygiene callouts. Pure read; email.py renders it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.close.anomalies import AnomalyReport, scan_anomalies
from src.close.reconcile import ReconcileSummary, prior_month, reconcile
from src.models.enums import TransactionStatus
from src.models.transaction import Transaction

logger = logging.getLogger(__name__)

DASHBOARD_BASE = "https://books.sparkry.ai"

# REQ-FIX-DAT-002: report-only data-hygiene callouts (never auto-actioned).
# The formerly-unnamed Vanguard $0 stub was NAMED 2026-07-08 (audited PATCH);
# what remains is the human archive decision.
DATA_HYGIENE_CALLOUTS: tuple[str, ...] = (
    "Vanguard $0 stub (named 2026-07-08) — confirm archive",
    "$50 Fidelity TOD — human closure decision",
)

_EXCLUDED_STATUSES = (
    TransactionStatus.REJECTED.value,
    TransactionStatus.SPLIT_PARENT.value,
)


# ── evidence links ────────────────────────────────────────────────────────


def needs_review_link(entity: str | None) -> str:
    ent = entity or ""
    return f"{DASHBOARD_BASE}/?status=needs_review&entity={quote(ent)}"


def vendor_link(vendor_key: str, month: str) -> str:
    return f"{DASHBOARD_BASE}/transactions?vendor={quote(vendor_key)}&month={quote(month)}"


def account_link(account_id: str) -> str:
    return f"{DASHBOARD_BASE}/wealth/accounts/{quote(account_id)}"


# ── auto-confirm month summary ────────────────────────────────────────────


@dataclass
class AutoConfirmVendor:
    vendor: str
    count: int
    total: Decimal


@dataclass
class AutoConfirmSummary:
    total: int
    by_vendor: list[AutoConfirmVendor] = field(default_factory=list)


# ── close report ──────────────────────────────────────────────────────────


@dataclass
class CloseReport:
    month: str
    generated_at: str
    rows_ingested: int
    needs_review_depth: int
    reconcile: ReconcileSummary
    anomalies: AnomalyReport
    autoconfirm: AutoConfirmSummary
    data_hygiene: list[str] = field(default_factory=lambda: list(DATA_HYGIENE_CALLOUTS))
    # REQ-SEL-001: the sellability section ships WITH the close email.
    sellability_text: str | None = None
    # REQ-UTX-005 (#59): quarter-to-date WA use-tax estimate on comped orders.
    use_tax_text: str | None = None


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _rows_ingested(session: Session, first: str, last: str) -> int:
    return (
        session.query(func.count(Transaction.id))
        .filter(
            Transaction.date >= first,
            Transaction.date <= last,
            Transaction.status.notin_(_EXCLUDED_STATUSES),
        )
        .scalar()
    ) or 0


def _needs_review_depth(session: Session) -> int:
    return (
        session.query(func.count(Transaction.id))
        .filter(Transaction.status == TransactionStatus.NEEDS_REVIEW.value)
        .scalar()
    ) or 0


def _autoconfirm_summary(session: Session, first: str, last: str) -> AutoConfirmSummary:
    rows = (
        session.query(Transaction)
        .filter(
            Transaction.date >= first,
            Transaction.date <= last,
            Transaction.confirmed_by.like("auto:rule:%"),
        )
        .all()
    )
    by_vendor: dict[str, list[Transaction]] = {}
    for r in rows:
        by_vendor.setdefault(r.description, []).append(r)
    vendors = [
        AutoConfirmVendor(
            vendor=vendor,
            count=len(group),
            total=sum(
                (_dec(g.amount).copy_abs() for g in group if g.amount is not None),
                Decimal("0"),
            ).quantize(Decimal("0.01")),
        )
        for vendor, group in sorted(by_vendor.items())
    ]
    return AutoConfirmSummary(total=len(rows), by_vendor=vendors)


def build_close_report(
    session: Session,
    month: str | None = None,
    *,
    today: date | None = None,
) -> CloseReport:
    """Assemble the full CloseReport for *month* (default = prior calendar month)."""
    today = today or date.today()
    month = month or prior_month(today)
    first, last = _month_bounds(month)

    # REQ-SEL-001: embed the sellability section. Lazy import (reports→close
    # would otherwise risk a cycle); a sellability failure degrades to a note,
    # never kills the close report.
    try:
        from src.reports.sellability import compute_sellability, render_sellability_section

        sellability_text: str | None = render_sellability_section(
            compute_sellability(session, today=today)
        )
    except Exception:  # noqa: BLE001 — close must ship even if SEL breaks
        logger.exception("sellability section failed; close report degrades")
        sellability_text = "Sellability section unavailable (compute error — check logs)"

    # REQ-UTX-005 (#59): quarter-to-date WA use-tax estimate on comped ($0)
    # BlackLine orders. Report-only; a compute error degrades to a note.
    try:
        from src.export.use_tax_estimate import (
            build_use_tax_summary,
            render_use_tax_section,
        )

        use_tax_text: str | None = render_use_tax_section(
            build_use_tax_summary(session, month)
        )
    except Exception:  # noqa: BLE001 — close must ship even if UTX breaks
        logger.exception("use-tax section failed; close report degrades")
        use_tax_text = "Use-tax section unavailable (compute error — check logs)"

    return CloseReport(
        month=month,
        generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        rows_ingested=_rows_ingested(session, first, last),
        needs_review_depth=_needs_review_depth(session),
        reconcile=reconcile(session, month, today=today),
        anomalies=scan_anomalies(session, month),
        autoconfirm=_autoconfirm_summary(session, first, last),
        sellability_text=sellability_text,
        use_tax_text=use_tax_text,
    )
