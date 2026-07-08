"""Tax-posture forecaster (REQ-TXF-001..004) — household MFJ.

Deterministic, Decimal-only, no-LLM quarterly projection: YTD actuals ->
linear annualization (with a seasonality guard + trailing-3-month
alternative) -> Schedule C (Sparkry) -> K-1 passthrough (BlackLine) -> SE tax
-> QBI -> WA B&O accrual -> MFJ federal bracket position -> 110% safe-harbor
set-aside ladder. Delivered via Resend (``src/reports/report_email.py``).

REQ-TXF-003: household inputs come from ``config/tax_profile.yaml``
(gitignored). Missing/unfilled -> business-only mode: Schedule C / K-1 / B&O
/ SE render fully; bracket position + safe harbor print "UNAVAILABLE".

Design spec: docs/superpowers/specs/2026-07-07-reporting-suite-design.md §4.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.api.routes.tax_export import INCOME_CATEGORIES, _fetch_transactions
from src.export.bno_tax import BO_CLASSIFICATION, BO_RATE
from src.models.enums import Entity
from src.reports.report_config import CONFIG_DIR, load_config, load_yaml, to_decimal
from src.reports.report_email import dispatch_report

__all__ = [
    "TXFData",
    "compute_txf",
    "render_report",
    "build_subject",
    "main",
]

DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[2] / "data" / "accounting.db")

_TRIGGER_MONTHS = (1, 4, 6, 9)
_TRIGGER_TO_QUARTER = {1: 4, 4: 1, 6: 2, 9: 3}

_SEASONALITY_SINGLE_MONTH_PCT = Decimal("40")
_SEASONALITY_MIN_DAYS = 60


def _today() -> date:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def _q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q0(d: Decimal) -> Decimal:
    return d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _fmt0(d: Decimal) -> str:
    return f"${_q0(d):,.0f}"


def _fmt0_signed(d: Decimal) -> str:
    return f"(${_q0(abs(d)):,.0f})" if d < 0 else _fmt0(d)


# ── Period anchor (design spec §2/§4.5) ─────────────────────────────────────


def period_anchor(today: date) -> tuple[str, str]:
    """Returns (occurrence_date ISO, quarter_label e.g. "2026-Q3").

    ``occurrence_date`` is the report's own period_start — the 1st of the
    most-recently-elapsed trigger month (Jan/Apr/Jun/Sep), 2 weeks ahead of
    an estimated-tax due date. On-demand/off-cycle CLI runs snap to the most
    recent trigger month <= today; a January-or-earlier run wraps to the
    prior year's September trigger.
    """
    candidates = [m for m in _TRIGGER_MONTHS if m <= today.month]
    if candidates:
        trigger_month = max(candidates)
        anchor_year = today.year
    else:
        trigger_month = 9
        anchor_year = today.year - 1
    occurrence_date = date(anchor_year, trigger_month, 1).isoformat()
    quarter = _TRIGGER_TO_QUARTER[trigger_month]
    label_year = anchor_year - 1 if trigger_month == 1 else anchor_year
    quarter_label = f"{label_year}-Q{quarter}"
    return occurrence_date, quarter_label


# ── TypedDicts ────────────────────────────────────────────────────────────


class EntityProjection(TypedDict):
    gross_receipts_ytd: Decimal
    gross_receipts_projected: Decimal
    gross_receipts_projected_alt: Decimal | None
    expenses_ytd: Decimal
    expenses_projected: Decimal
    net_projected: Decimal


class SETaxData(TypedDict):
    se_base: Decimal
    ss_tax: Decimal
    medicare_tax: Decimal
    addl_medicare_tax: Decimal
    total: Decimal
    half_deduction: Decimal


class QBIData(TypedDict):
    business_income: Decimal
    before_phaseout: Decimal
    is_sstb: bool
    phase_out_fraction: Decimal
    cap: Decimal
    final: Decimal


class BracketWalkRow(TypedDict):
    up_to: Decimal
    rate: Decimal
    tax_in_bracket: Decimal


class BracketData(TypedDict):
    available: bool
    taxable_income: Decimal
    total_tax: Decimal
    marginal_rate: Decimal
    effective_rate: Decimal | None
    distance_to_next_edge: Decimal | None
    rows: list[BracketWalkRow]


class SafeHarborLine(TypedDict):
    due_date: str
    set_aside: Decimal


class SafeHarborData(TypedDict):
    available: bool
    target: Decimal
    paid_to_date: Decimal
    lines: list[SafeHarborLine]


class BnoRow(TypedDict):
    entity: str
    code: str
    label: str
    projected_gross: Decimal
    rate: Decimal
    annual_tax: Decimal
    per_period_tax: Decimal
    cadence: str


class TXFData(TypedDict):
    year: int
    as_of: str
    days_elapsed: int
    days_in_year: int
    seasonality_guard: bool
    seasonality_reason: str | None
    sparkry: EntityProjection
    blackline: EntityProjection
    se_tax: SETaxData
    qbi: QBIData
    bno_rows: list[BnoRow]
    bracket: BracketData
    safe_harbor: SafeHarborData
    business_only: bool
    business_only_reason: str | None
    tax_tables_path: str
    tax_profile_path: str
    tax_tables_mtime: str | None
    tax_profile_mtime: str | None
    quarter_label: str


# ── Config defaults (release-blocking placeholders — see config/tax_tables/2026.yaml) ──

_TAX_TABLE_DEFAULTS: dict[str, Any] = {
    "tax_year": 2026,
    "home_office_deduction": "1500.00",
    "k1_share": "1.0",
    "se_tax_rate": "0.9235",
    "ss_rate": "0.124",
    "medicare_rate": "0.029",
    "addl_medicare_rate": "0.009",
    "ss_wage_base": "176100.00",
    "addl_medicare_mfj_threshold": "250000.00",
    "qbi_rate": "0.20",
    "qbi_mfj_threshold": "383900.00",
    "qbi_mfj_phase_out_width": "100000.00",
    "qbi_is_sstb": True,
    "standard_deduction_mfj": "30000.00",
    "mfj_brackets": [
        {"up_to": "23850.00", "rate": "0.10"},
        {"up_to": "96950.00", "rate": "0.12"},
        {"up_to": "206700.00", "rate": "0.22"},
        {"up_to": "394600.00", "rate": "0.24"},
        {"up_to": "501050.00", "rate": "0.32"},
        {"up_to": "751600.00", "rate": "0.35"},
        {"up_to": "99999999.00", "rate": "0.37"},
    ],
    "safe_harbor_pct": "1.10",
    "estimated_tax_due_dates": [
        {"quarter": 1, "due_date": "2026-04-15"},
        {"quarter": 2, "due_date": "2026-06-15"},
        {"quarter": 3, "due_date": "2026-09-15"},
        {"quarter": 4, "due_date": "2027-01-15"},
    ],
}


# ── Category math (reuses /tax-summary's exact predicate — REQ-TXF-001) ──


def _month_totals(transactions: list[Any], categories: set[str], *, income: bool) -> dict[int, Decimal]:
    """1-based month -> abs(amount) * deductible_pct summed over transactions
    whose tax_category is in *categories*."""
    out: dict[int, Decimal] = {}
    for tx in transactions:
        cat = tx.tax_category
        if cat not in categories:
            continue
        date_str = tx.date or ""
        try:
            month = int(date_str[5:7])
        except (IndexError, ValueError):
            continue
        amt = Decimal(str(tx.amount)) if tx.amount is not None else Decimal("0")
        pct = Decimal(str(tx.deductible_pct))
        out[month] = out.get(month, Decimal("0")) + abs(amt) * pct
    return out


def _category_totals(transactions: list[Any], categories: set[str]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for tx in transactions:
        cat = tx.tax_category
        if cat not in categories:
            continue
        amt = Decimal(str(tx.amount)) if tx.amount is not None else Decimal("0")
        pct = Decimal(str(tx.deductible_pct))
        out[cat] = out.get(cat, Decimal("0")) + abs(amt) * pct
    return out


_EXPENSE_EXCLUDE = {"PERSONAL_NON_DEDUCTIBLE", "CAPITAL_CONTRIBUTION", "REIMBURSABLE", *INCOME_CATEGORIES}


def _linear_project(ytd: Decimal, days_in_year: int, days_elapsed: int) -> Decimal:
    if days_elapsed <= 0:
        return Decimal("0")
    return ytd * Decimal(days_in_year) / Decimal(days_elapsed)


def _seasonality_guard(month_totals: dict[int, Decimal], ytd_total: Decimal, days_elapsed: int) -> tuple[bool, str | None]:
    if days_elapsed < _SEASONALITY_MIN_DAYS:
        return True, f"YTD spans only {days_elapsed}d (< {_SEASONALITY_MIN_DAYS}d minimum)"
    if ytd_total <= 0:
        return False, None
    biggest_month = max(month_totals.values()) if month_totals else Decimal("0")
    pct = biggest_month / ytd_total * Decimal("100")
    if pct > _SEASONALITY_SINGLE_MONTH_PCT:
        return True, f"a single month holds {pct.quantize(Decimal('0.1'))}% of YTD gross receipts (> {_SEASONALITY_SINGLE_MONTH_PCT}%)"
    return False, None


def _trailing_3mo_alt(ytd_gross: Decimal, month_totals: dict[int, Decimal], today: date) -> Decimal:
    """Trailing-3-month run-rate x remaining months, added to YTD actuals
    (design spec §4.1 seasonality-guard alternative). Only applied to the
    Sparkry gross-receipts figure; SE/QBI/B&O/bracket math downstream always
    uses the primary linear projection — the alternative is informational."""
    trailing_months = [m for m in range(max(1, today.month - 3), today.month)]
    if not trailing_months:
        trailing_months = [today.month]
    trailing_sum = sum((month_totals.get(m, Decimal("0")) for m in trailing_months), Decimal("0"))
    avg_monthly = trailing_sum / len(trailing_months)
    remaining_months = max(0, 12 - today.month)
    return ytd_gross + avg_monthly * Decimal(remaining_months)


def _project_entity(
    session: Session, entity: str, year: int, today: date, days_in_year: int, days_elapsed: int
) -> tuple[EntityProjection, bool, str | None]:
    transactions = _fetch_transactions(session, entity, year)
    income_totals = _category_totals(transactions, INCOME_CATEGORIES)
    gross_ytd = sum(income_totals.values(), Decimal("0"))
    expense_categories = {
        cat
        for cat in {tx.tax_category for tx in transactions}
        if cat and cat not in _EXPENSE_EXCLUDE
    }
    expense_totals = _category_totals(transactions, expense_categories)
    expenses_ytd = sum(expense_totals.values(), Decimal("0"))

    gross_projected = _linear_project(gross_ytd, days_in_year, days_elapsed)
    expenses_projected = _linear_project(expenses_ytd, days_in_year, days_elapsed)

    month_totals = _month_totals(transactions, INCOME_CATEGORIES, income=True)
    guard, reason = _seasonality_guard(month_totals, gross_ytd, days_elapsed)
    alt = _trailing_3mo_alt(gross_ytd, month_totals, today) if guard else None

    net_projected = gross_projected - expenses_projected

    row: EntityProjection = {
        "gross_receipts_ytd": _q2(gross_ytd),
        "gross_receipts_projected": _q2(gross_projected),
        "gross_receipts_projected_alt": _q2(alt) if alt is not None else None,
        "expenses_ytd": _q2(expenses_ytd),
        "expenses_projected": _q2(expenses_projected),
        "net_projected": _q2(net_projected),
    }
    return row, guard, reason


# ── SE tax / QBI / brackets / safe harbor ────────────────────────────────


def _compute_se_tax(business_net: Decimal, w2_wages: Decimal, cfg: dict[str, Decimal]) -> SETaxData:
    se_base = max(Decimal("0"), business_net) * cfg["se_tax_rate"]
    ss_room = max(Decimal("0"), cfg["ss_wage_base"] - w2_wages)
    ss_taxable = min(se_base, ss_room)
    ss_tax = ss_taxable * cfg["ss_rate"]
    medicare_tax = se_base * cfg["medicare_rate"]
    combined = w2_wages + se_base
    over_threshold = max(Decimal("0"), combined - cfg["addl_medicare_mfj_threshold"])
    addl_medicare_taxable = min(se_base, over_threshold)
    addl_medicare_tax = addl_medicare_taxable * cfg["addl_medicare_rate"]
    total = ss_tax + medicare_tax + addl_medicare_tax
    half_deduction = (ss_tax + medicare_tax) / 2
    return {
        "se_base": _q2(se_base),
        "ss_tax": _q2(ss_tax),
        "medicare_tax": _q2(medicare_tax),
        "addl_medicare_tax": _q2(addl_medicare_tax),
        "total": _q2(total),
        "half_deduction": _q2(half_deduction),
    }


def _compute_qbi(
    business_income: Decimal,
    taxable_income_before_qbi: Decimal,
    capital_gains_lt: Decimal,
    cfg: dict[str, Decimal],
    is_sstb: bool,
) -> QBIData:
    before_phaseout = max(Decimal("0"), business_income) * cfg["qbi_rate"]
    fraction = Decimal("0")
    if is_sstb and taxable_income_before_qbi > cfg["qbi_mfj_threshold"]:
        width = cfg["qbi_mfj_phase_out_width"]
        fraction = min(Decimal("1"), (taxable_income_before_qbi - cfg["qbi_mfj_threshold"]) / width) if width > 0 else Decimal("1")
    reduced = before_phaseout * (Decimal("1") - fraction)
    cap = cfg["qbi_rate"] * max(Decimal("0"), taxable_income_before_qbi - capital_gains_lt)
    final = max(Decimal("0"), min(reduced, cap))
    return {
        "business_income": _q2(business_income),
        "before_phaseout": _q2(before_phaseout),
        "is_sstb": is_sstb,
        "phase_out_fraction": fraction.quantize(Decimal("0.01")),
        "cap": _q2(cap),
        "final": _q2(final),
    }


def _walk_brackets(taxable_income: Decimal, brackets: list[dict[str, Decimal]]) -> BracketData:
    if taxable_income <= 0:
        return {
            "available": True,
            "taxable_income": _q2(taxable_income),
            "total_tax": Decimal("0.00"),
            "marginal_rate": brackets[0]["rate"] if brackets else Decimal("0"),
            "effective_rate": None,
            "distance_to_next_edge": brackets[0]["up_to"] if brackets else None,
            "rows": [],
        }
    total_tax = Decimal("0")
    prev_ceiling = Decimal("0")
    marginal_rate = brackets[-1]["rate"] if brackets else Decimal("0")
    distance_to_next: Decimal | None = None
    rows: list[BracketWalkRow] = []
    for b in brackets:
        ceiling = b["up_to"]
        rate = b["rate"]
        if taxable_income <= prev_ceiling:
            break
        band_top = min(taxable_income, ceiling)
        band_amount = max(Decimal("0"), band_top - prev_ceiling)
        tax_in_bracket = band_amount * rate
        total_tax += tax_in_bracket
        rows.append({"up_to": ceiling, "rate": rate, "tax_in_bracket": _q2(tax_in_bracket)})
        if taxable_income <= ceiling:
            marginal_rate = rate
            distance_to_next = ceiling - taxable_income
            break
        prev_ceiling = ceiling
    effective_rate = (total_tax / taxable_income) if taxable_income > 0 else None
    return {
        "available": True,
        "taxable_income": _q2(taxable_income),
        "total_tax": _q2(total_tax),
        "marginal_rate": marginal_rate,
        "effective_rate": effective_rate.quantize(Decimal("0.0001")) if effective_rate is not None else None,
        "distance_to_next_edge": _q2(distance_to_next) if distance_to_next is not None else None,
        "rows": rows,
    }


def _compute_safe_harbor(
    prior_year_total_tax: Decimal,
    w2_withholding: Decimal,
    estimated_payments: list[dict[str, Any]],
    due_dates: list[dict[str, Any]],
    safe_harbor_pct: Decimal,
    today: date,
) -> SafeHarborData:
    target = prior_year_total_tax * safe_harbor_pct
    paid_to_date = w2_withholding + sum(
        (to_decimal(p["amount"]) for p in estimated_payments if date.fromisoformat(str(p["date"])) <= today),
        Decimal("0"),
    )
    lines: list[SafeHarborLine] = []
    for entry in due_dates:
        due = date.fromisoformat(str(entry["due_date"]))
        if due <= today:
            continue
        quarter = int(entry["quarter"])
        cumulative_fraction = Decimal(quarter) / Decimal(4)
        required_by_then = target * cumulative_fraction
        set_aside = max(Decimal("0"), required_by_then - paid_to_date)
        lines.append({"due_date": due.isoformat(), "set_aside": _q0(set_aside)})
    return {
        "available": True,
        "target": _q2(target),
        "paid_to_date": _q2(paid_to_date),
        "lines": lines,
    }


_UNAVAILABLE_BRACKET: BracketData = {
    "available": False,
    "taxable_income": Decimal("0"),
    "total_tax": Decimal("0"),
    "marginal_rate": Decimal("0"),
    "effective_rate": None,
    "distance_to_next_edge": None,
    "rows": [],
}
_UNAVAILABLE_SAFE_HARBOR: SafeHarborData = {
    "available": False,
    "target": Decimal("0"),
    "paid_to_date": Decimal("0"),
    "lines": [],
}


# ── B&O accrual ──────────────────────────────────────────────────────────


def _project_bno(
    session: Session, entity: str, year: int, today: date, days_in_year: int, days_elapsed: int, cadence: str, periods_per_year: int
) -> list[BnoRow]:
    transactions = _fetch_transactions(session, entity, year)
    income_totals = _category_totals(transactions, INCOME_CATEGORIES)
    rows: list[BnoRow] = []
    for category, ytd in sorted(income_totals.items()):
        projected = _linear_project(ytd, days_in_year, days_elapsed)
        code, label = BO_CLASSIFICATION.get(category, ("ServiceOther", "Service and Other Activities"))
        rate = BO_RATE.get(code, Decimal("0.015"))
        annual_tax = projected * rate
        rows.append(
            {
                "entity": entity,
                "code": code,
                "label": label,
                "projected_gross": _q2(projected),
                "rate": rate,
                "annual_tax": _q2(annual_tax),
                "per_period_tax": _q2(annual_tax / periods_per_year),
                "cadence": cadence,
            }
        )
    return rows


# ── Top-level compute ────────────────────────────────────────────────────


def compute_txf(session: Session, today: date | None = None, *, config_dir: Path | None = None) -> TXFData:
    today = today or _today()
    cfg_dir = config_dir or CONFIG_DIR

    tables_cfg = load_config("tax_tables/2026.yaml", _TAX_TABLE_DEFAULTS, config_dir=cfg_dir)
    year = int(tables_cfg.get("tax_year", today.year))

    brackets = [{"up_to": to_decimal(b["up_to"]), "rate": to_decimal(b["rate"])} for b in tables_cfg["mfj_brackets"]]
    cfg: dict[str, Decimal] = {
        "k1_share": to_decimal(tables_cfg["k1_share"]),
        "se_tax_rate": to_decimal(tables_cfg["se_tax_rate"]),
        "ss_rate": to_decimal(tables_cfg["ss_rate"]),
        "medicare_rate": to_decimal(tables_cfg["medicare_rate"]),
        "addl_medicare_rate": to_decimal(tables_cfg["addl_medicare_rate"]),
        "ss_wage_base": to_decimal(tables_cfg["ss_wage_base"]),
        "addl_medicare_mfj_threshold": to_decimal(tables_cfg["addl_medicare_mfj_threshold"]),
        "qbi_rate": to_decimal(tables_cfg["qbi_rate"]),
        "qbi_mfj_threshold": to_decimal(tables_cfg["qbi_mfj_threshold"]),
        "qbi_mfj_phase_out_width": to_decimal(tables_cfg["qbi_mfj_phase_out_width"]),
        "standard_deduction_mfj": to_decimal(tables_cfg["standard_deduction_mfj"]),
        "safe_harbor_pct": to_decimal(tables_cfg["safe_harbor_pct"]),
        "home_office_deduction": to_decimal(tables_cfg["home_office_deduction"]),
    }
    is_sstb = bool(tables_cfg.get("qbi_is_sstb", True))

    days_in_year = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    days_elapsed = min(days_in_year, (today - date(year, 1, 1)).days + 1)

    sparkry, sp_guard, sp_reason = _project_entity(session, Entity.SPARKRY.value, year, today, days_in_year, days_elapsed)
    blackline, bl_guard, bl_reason = _project_entity(session, Entity.BLACKLINE.value, year, today, days_in_year, days_elapsed)
    guard = sp_guard or bl_guard
    reason = sp_reason or bl_reason

    sparkry_net = sparkry["net_projected"] - cfg["home_office_deduction"]
    k1_net = blackline["net_projected"] * cfg["k1_share"]
    business_net_total = sparkry_net + k1_net

    bno_rows = _project_bno(session, Entity.SPARKRY.value, year, today, days_in_year, days_elapsed, "monthly", 12)
    bno_rows += _project_bno(session, Entity.BLACKLINE.value, year, today, days_in_year, days_elapsed, "quarterly", 4)

    tax_profile_path = cfg_dir / "tax_profile.yaml"
    profile = load_yaml(tax_profile_path)
    prior_year_total_tax = to_decimal(profile.get("prior_year_total_tax", 0)) if profile else Decimal("0")
    business_only = not profile or prior_year_total_tax <= 0
    business_only_reason = None
    if not profile:
        business_only_reason = "config/tax_profile.yaml not found"
    elif prior_year_total_tax <= 0:
        business_only_reason = "prior_year_total_tax not set (<= 0) in config/tax_profile.yaml"

    w2_rows = profile.get("w2", []) if profile else []
    w2_wages = sum((to_decimal(w.get("ytd_wages", 0)) for w in w2_rows), Decimal("0"))
    w2_ss_wages = sum((to_decimal(w.get("ytd_ss_wages", 0)) for w in w2_rows), Decimal("0"))
    w2_withholding = sum((to_decimal(w.get("ytd_federal_withholding", 0)) for w in w2_rows), Decimal("0"))
    investment = profile.get("expected_investment_income", {}) if profile else {}
    investment_income = sum((to_decimal(v) for v in investment.values()), Decimal("0")) if investment else Decimal("0")
    capital_gains_lt = to_decimal(investment.get("capital_gains_lt", 0)) if investment else Decimal("0")

    se_tax = _compute_se_tax(business_net_total, w2_ss_wages, cfg)

    taxable_income_before_qbi = max(
        Decimal("0"),
        business_net_total - se_tax["half_deduction"] + w2_wages + investment_income - cfg["standard_deduction_mfj"],
    )
    qbi = _compute_qbi(business_net_total, taxable_income_before_qbi, capital_gains_lt, cfg, is_sstb)

    if business_only:
        bracket = _UNAVAILABLE_BRACKET
        safe_harbor = _UNAVAILABLE_SAFE_HARBOR
    else:
        taxable_income = max(Decimal("0"), taxable_income_before_qbi - qbi["final"])
        bracket = _walk_brackets(taxable_income, brackets)
        estimated_payments = profile.get("estimated_payments", []) if profile else []
        due_dates = tables_cfg["estimated_tax_due_dates"]
        safe_harbor = _compute_safe_harbor(
            prior_year_total_tax, w2_withholding, estimated_payments, due_dates, cfg["safe_harbor_pct"], today
        )

    _, quarter_label = period_anchor(today)

    tables_mtime = None
    if tables_cfg.path.exists():
        tables_mtime = datetime.fromtimestamp(tables_cfg.path.stat().st_mtime).isoformat(timespec="seconds")
    profile_mtime = None
    if tax_profile_path.exists():
        profile_mtime = datetime.fromtimestamp(tax_profile_path.stat().st_mtime).isoformat(timespec="seconds")

    return {
        "year": year,
        "as_of": today.isoformat(),
        "days_elapsed": days_elapsed,
        "days_in_year": days_in_year,
        "seasonality_guard": guard,
        "seasonality_reason": reason,
        "sparkry": sparkry,
        "blackline": blackline,
        "se_tax": se_tax,
        "qbi": qbi,
        "bno_rows": bno_rows,
        "bracket": bracket,
        "safe_harbor": safe_harbor,
        "business_only": business_only,
        "business_only_reason": business_only_reason,
        "tax_tables_path": str(tables_cfg.path),
        "tax_profile_path": str(tax_profile_path),
        "tax_tables_mtime": tables_mtime,
        "tax_profile_mtime": profile_mtime,
        "quarter_label": quarter_label,
    }


def build_subject(data: TXFData) -> str:
    if data["safe_harbor"]["available"] and data["safe_harbor"]["lines"]:
        next_line = data["safe_harbor"]["lines"][0]
        sh = data["safe_harbor"]
        pct_paid = (sh["paid_to_date"] / sh["target"] * 100) if sh["target"] > 0 else Decimal("0")
        return (
            f"[TAX] {data['quarter_label']} forecast · set aside {_fmt0(next_line['set_aside'])} "
            f"by {next_line['due_date']} · SH 110%: {_fmt0(sh['paid_to_date'])} of {_fmt0(sh['target'])} paid "
            f"({pct_paid.quantize(Decimal('1'))}%)"
        )
    return f"[TAX] {data['quarter_label']} forecast · business-only (fill config/tax_profile.yaml for SH tracking)"


def render_report(data: TXFData) -> str:
    out: list[str] = []
    out.append(f"TAX FORECAST — {data['quarter_label']} · tax year {data['year']} · as of {data['as_of']}")
    out.append("")
    out.append("ASSUMPTIONS")
    out.append("  method: linear YTD annualization (projected = ytd × days_in_year / days_elapsed)")
    out.append(f"  days elapsed: {data['days_elapsed']} / {data['days_in_year']}")
    if data["seasonality_guard"]:
        out.append(f"  ⚠️ HIGH VARIANCE — {data['seasonality_reason']}; trailing-3-month alternative shown alongside gross receipts")
    else:
        out.append("  seasonality guard: not tripped")
    out.append(f"  tax_tables: {data['tax_tables_path']} (mtime {data['tax_tables_mtime'] or 'n/a'})")
    out.append(f"  tax_profile: {data['tax_profile_path']} (mtime {data['tax_profile_mtime'] or 'not found'})")
    if data["business_only"]:
        out.append(f"  ⚠️ BUSINESS-ONLY MODE — {data['business_only_reason']}")
    out.append("  projection ≠ advice; deterministic annualization of cash-basis actuals.")

    out.append("")
    out.append("SCHEDULE C (Sparkry)")
    sp = data["sparkry"]
    alt = f"  (alt trailing-3mo: {_fmt0(sp['gross_receipts_projected_alt'])})" if sp["gross_receipts_projected_alt"] is not None else ""
    out.append(f"  gross receipts   ytd {_fmt0(sp['gross_receipts_ytd'])}   projected {_fmt0(sp['gross_receipts_projected'])}{alt}")
    out.append(f"  expenses         ytd {_fmt0(sp['expenses_ytd'])}   projected {_fmt0(sp['expenses_projected'])}")
    out.append(f"  net profit (projected, after home-office): {_fmt0_signed(sp['net_projected'])}")

    out.append("")
    out.append("1065 / K-1 (BlackLine)")
    bl = data["blackline"]
    out.append(f"  entity net projected: {_fmt0_signed(bl['net_projected'])}   K-1 share applied downstream")

    out.append("")
    out.append("SE TAX")
    se = data["se_tax"]
    out.append(f"  SE base {_fmt0(se['se_base'])}   SS {_fmt0(se['ss_tax'])}   Medicare {_fmt0(se['medicare_tax'])}   Addl Medicare {_fmt0(se['addl_medicare_tax'])}")
    out.append(f"  total SE tax {_fmt0(se['total'])}   (½-SE deduction {_fmt0(se['half_deduction'])})")

    out.append("")
    out.append("QBI")
    qbi = data["qbi"]
    sstb_note = " (SSTB)" if qbi["is_sstb"] else ""
    out.append(f"  20% × business income{sstb_note}: {_fmt0(qbi['before_phaseout'])}   phase-out fraction: {qbi['phase_out_fraction']}")
    out.append(f"  cap (20% × taxable income − cap gains): {_fmt0(qbi['cap'])}")
    out.append(f"  QBI deduction: {_fmt0(qbi['final'])}")

    out.append("")
    out.append("WA B&O ACCRUAL")
    for row in data["bno_rows"]:
        out.append(
            f"  {row['entity']:<10} {row['label']:<28} projected gross {_fmt0(row['projected_gross']):<10} "
            f"rate {row['rate'] * 100}%   {row['cadence']} accrual {_fmt0(row['per_period_tax'])}"
        )

    out.append("")
    out.append("MFJ FEDERAL BRACKET POSITION")
    br = data["bracket"]
    if not br["available"]:
        out.append("  UNAVAILABLE — fill config/tax_profile.yaml")
    else:
        out.append(f"  taxable income: {_fmt0(br['taxable_income'])}   total tax: {_fmt0(br['total_tax'])}")
        eff = f"{(br['effective_rate'] * 100).quantize(Decimal('0.1'))}%" if br["effective_rate"] is not None else "n/a"
        out.append(f"  marginal rate: {(br['marginal_rate'] * 100).quantize(Decimal('0.1'))}%   effective rate: {eff}")
        if br["distance_to_next_edge"] is not None:
            out.append(f"  distance to next bracket edge: {_fmt0(br['distance_to_next_edge'])}")

    out.append("")
    out.append("SAFE HARBOR (110% of prior-year total tax)")
    sh = data["safe_harbor"]
    if not sh["available"]:
        out.append("  UNAVAILABLE — fill config/tax_profile.yaml")
    else:
        out.append(f"  target: {_fmt0(sh['target'])}   paid to date: {_fmt0(sh['paid_to_date'])}")
        if not sh["lines"]:
            out.append("  no remaining estimated-tax due dates this cycle.")
        for line in sh["lines"]:
            out.append(f"  Set aside {_fmt0(line['set_aside'])} by {line['due_date']}")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tax_forecast_dispatch", description="Tax-posture forecaster (REQ-TXF-001..004).")
    parser.add_argument("--apply", action="store_true", help="Send via Resend + record in the ledger (default: DRY-RUN).")
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="Override today's date (YYYY-MM-DD).")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    today = args.date or _today()
    engine = create_engine(f"sqlite:///{args.db}")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        data = compute_txf(session, today)
        body = render_report(data)
        subject = build_subject(data)
        if not args.apply:
            print(body)
            return 0
        occurrence_date, _ = period_anchor(today)
        result = dispatch_report(
            session,
            alert_key=f"txf:{data['quarter_label']}",
            occurrence_date=occurrence_date,
            alert_type="tax_forecast",
            entity="all",
            subject=subject,
            body=body,
            apply=True,
        )
        print(f"[{result.status}] {subject}")
        return 1 if result.status == "failed" else 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
