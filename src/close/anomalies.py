"""Deterministic monthly-close anomaly scan (REQ-MCA-001, spec §1.3).

Three detectors, all pure functions of the register + a checked-in config:
  - new vendors first seen in the close month,
  - per-vendor amount outliers (z-score),
  - missing expected-recurring charges (history-derived, config-overridable).

No LLM. ``normalize_vendor`` is exhaustively unit-tested; every statistic uses
``Decimal(str(...))`` at the boundary and only drops to float inside the
z-score / median math (never persisted).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from src.models.enums import TransactionStatus
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

_CENTS = Decimal("0.01")
_NEW_VENDOR_MIN = Decimal("25")
_OUTLIER_MIN_ABS = Decimal("50")
_OUTLIER_Z = 3.0
_SIGMA_FLOOR = 1.0

# Default config lives at repo-root config/close_recurring.yaml. parents[2] is
# the repo root (src/close/anomalies.py → close → src → root).
_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "close_recurring.yaml"

# A trailing token is a reference/id suffix (stripped by normalize_vendor) when
# it is: a #ref, a *-prefixed number, a pure/dotted/hyphenated number, or an
# alphanumeric blob carrying >= 4 consecutive digits (transaction ids).
_REF_TOKEN = re.compile(r"^(?:#\S*|\*+\d+|\d[\d.\-]*|[a-z]*\d{4,}[a-z0-9]*)$")


def normalize_vendor(description: str | None) -> str:
    """Collapse a raw description to a stable vendor key.

    Lower-cases (casefold), collapses internal whitespace, and strips trailing
    reference/id suffixes. If stripping would empty the string (e.g. the whole
    description is a number), the collapsed form is returned unchanged.
    """
    if not description:
        return ""
    collapsed = re.sub(r"\s+", " ", description.casefold()).strip()
    if not collapsed:
        return ""
    tokens = collapsed.split(" ")
    while len(tokens) > 1 and _REF_TOKEN.match(tokens[-1]):
        tokens.pop()
    stripped = " ".join(tokens).strip()
    return stripped if stripped else collapsed


# ── result types ──────────────────────────────────────────────────────────


@dataclass
class NewVendor:
    vendor_key: str
    entity: str | None
    count: int
    total: Decimal


@dataclass
class AmountOutlier:
    vendor_key: str
    entity: str | None
    transaction_id: str
    date: str
    amount: Decimal
    mean: Decimal
    std: Decimal
    z_score: float


@dataclass
class MissingRecurring:
    vendor_key: str
    entity: str | None
    last_seen: str | None
    typical_amount: Decimal
    source: str  # "history" | "config"


@dataclass
class AnomalyReport:
    month: str
    new_vendors: list[NewVendor] = field(default_factory=list)
    outliers: list[AmountOutlier] = field(default_factory=list)
    missing_recurring: list[MissingRecurring] = field(default_factory=list)


# ── month helpers ─────────────────────────────────────────────────────────


def _month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def _months_before(anchor: date, n: int) -> date:
    total = anchor.year * 12 + (anchor.month - 1) - n
    return date(total // 12, total % 12 + 1, 1)


def _abs(amount: Any) -> Decimal:
    return abs(Decimal(str(amount)))


# ── config ────────────────────────────────────────────────────────────────


@dataclass
class _RecurringConfig:
    ignore: set[str]
    require: list[dict[str, Any]]


def _load_config(config_path: Path | None) -> _RecurringConfig:
    path = config_path or _DEFAULT_CONFIG
    if not path.exists():
        return _RecurringConfig(ignore=set(), require=[])
    raw = yaml.safe_load(path.read_text()) or {}
    ignore_raw = raw.get("ignore") or []
    require_raw = raw.get("require") or []
    ignore = {normalize_vendor(str(v)) for v in ignore_raw}
    require = [dict(r) for r in require_raw if isinstance(r, dict)]
    return _RecurringConfig(ignore=ignore, require=require)


# ── vendor-rule matching (read-only; never mutates last_matched) ───────────


def _rule_matches(rules: list[VendorRule], description: str) -> bool:
    desc = description.lower()
    for rule in rules:
        pattern = rule.vendor_pattern
        try:
            if rule.is_regex:
                found = re.search(pattern, desc, re.IGNORECASE)
            else:
                found = re.search(re.escape(pattern), desc, re.IGNORECASE)
        except re.error:
            continue
        if found:
            return True
    return False


# ── the scan ──────────────────────────────────────────────────────────────


def _load_rows(session: Session) -> list[Transaction]:
    return (
        session.query(Transaction)
        .filter(
            Transaction.amount.is_not(None),
            Transaction.status.notin_(
                [TransactionStatus.REJECTED.value, TransactionStatus.SPLIT_PARENT.value]
            ),
        )
        .all()
    )


def scan_anomalies(
    session: Session,
    month: str,
    *,
    config_path: Path | None = None,
) -> AnomalyReport:
    """Run all three deterministic detectors for the close *month* (``YYYY-MM``)."""
    first, last = _month_bounds(month)
    first_iso, last_iso = first.isoformat(), last.isoformat()
    prior12_iso = _months_before(first, 12).isoformat()
    prior6_iso = _months_before(first, 6).isoformat()
    config = _load_config(config_path)

    rows = _load_rows(session)
    rules: list[VendorRule] = session.query(VendorRule).all()

    by_key: dict[str, list[Transaction]] = {}
    for r in rows:
        by_key.setdefault(normalize_vendor(r.description), []).append(r)

    report = AnomalyReport(month=month)

    for key, group in sorted(by_key.items()):
        if not key:
            continue
        dates = [g.date for g in group]
        close_rows = [g for g in group if first_iso <= g.date <= last_iso]
        earliest = min(dates)
        entity = close_rows[0].entity if close_rows else group[0].entity

        # ── new vendors ──
        if close_rows and earliest >= first_iso and not _rule_matches(
            rules, close_rows[0].description
        ):
            qualifying = [g for g in close_rows if _abs(g.amount) >= _NEW_VENDOR_MIN]
            if qualifying:
                total = sum((_abs(g.amount) for g in qualifying), Decimal("0"))
                report.new_vendors.append(
                    NewVendor(
                        vendor_key=key,
                        entity=entity,
                        count=len(qualifying),
                        total=total.quantize(_CENTS),
                    )
                )

        # ── amount outliers ──
        prior12 = [g for g in group if prior12_iso <= g.date < first_iso]
        if len(prior12) >= 5 and close_rows:
            vals = [float(_abs(g.amount)) for g in prior12]
            mu = statistics.fmean(vals)
            sigma = statistics.pstdev(vals)
            sigma_used = max(sigma, _SIGMA_FLOOR)
            for g in close_rows:
                amt = float(_abs(g.amount))
                diff = abs(amt - mu)
                z = diff / sigma_used
                if z >= _OUTLIER_Z and Decimal(str(diff)) >= _OUTLIER_MIN_ABS:
                    report.outliers.append(
                        AmountOutlier(
                            vendor_key=key,
                            entity=g.entity,
                            transaction_id=g.id,
                            date=g.date,
                            amount=Decimal(str(g.amount)).quantize(_CENTS),
                            mean=Decimal(str(mu)).quantize(_CENTS),
                            std=Decimal(str(sigma)).quantize(_CENTS),
                            z_score=round(z, 2),
                        )
                    )

        # ── missing expected recurring (history-derived) ──
        if key in config.ignore:
            continue
        prior6 = sorted(
            (g for g in group if prior6_iso <= g.date < first_iso), key=lambda g: g.date
        )
        if len(prior6) >= 3 and not close_rows:
            day_dates = sorted({date.fromisoformat(g.date) for g in prior6})
            intervals = [
                (day_dates[i + 1] - day_dates[i]).days for i in range(len(day_dates) - 1)
            ]
            if intervals and 25 <= statistics.median(intervals) <= 35:
                typical = statistics.median([_abs(g.amount) for g in prior6])
                report.missing_recurring.append(
                    MissingRecurring(
                        vendor_key=key,
                        entity=entity,
                        last_seen=prior6[-1].date,
                        typical_amount=Decimal(str(typical)).quantize(_CENTS),
                        source="history",
                    )
                )

    # ── config require: force-track even when history is thin ──
    already = {m.vendor_key for m in report.missing_recurring}
    for entry in config.require:
        vendor = normalize_vendor(str(entry.get("vendor", "")))
        if not vendor or vendor in already:
            continue
        group = by_key.get(vendor, [])
        close_rows = [g for g in group if first_iso <= g.date <= last_iso]
        if close_rows:
            continue  # charge present this month — not missing
        hint = entry.get("amount_hint")
        typical = Decimal(str(hint)).quantize(_CENTS) if hint else Decimal("0.00")
        last_seen = max((g.date for g in group), default=None)
        report.missing_recurring.append(
            MissingRecurring(
                vendor_key=vendor,
                entity=group[0].entity if group else None,
                last_seen=last_seen,
                typical_amount=typical,
                source="config",
            )
        )

    return report
