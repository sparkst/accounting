"""Tests for src/reports/tax_forecast.py — tax-posture forecaster.

REQ-ID: REQ-TXF-001..004.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import resend as _resend
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401 — registers all ORM models before create_all
from src.alerts.models import AlertDispatch
from src.models.base import Base
from src.models.enums import ConfirmedBy, Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction
from src.reports import report_email
from src.reports import tax_forecast as txf

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "reports-golden" / "txf-basic.txt"
TODAY = date(2026, 7, 7)  # matches CLAUDE.md's currentDate for this session

_TABLES_YAML = """\
tax_year: 2026
home_office_deduction: 100.00
k1_share: 1.0
se_tax_rate: 0.9235
ss_rate: 0.124
medicare_rate: 0.029
addl_medicare_rate: 0.009
ss_wage_base: 176100.00
addl_medicare_mfj_threshold: 250000.00
qbi_rate: 0.20
qbi_mfj_threshold: 383900.00
qbi_mfj_phase_out_width: 100000.00
qbi_is_sstb: true
standard_deduction_mfj: 30000.00
mfj_brackets:
  - {up_to: 23850.00, rate: 0.10}
  - {up_to: 96950.00, rate: 0.12}
  - {up_to: 206700.00, rate: 0.22}
  - {up_to: 394600.00, rate: 0.24}
  - {up_to: 501050.00, rate: 0.32}
  - {up_to: 751600.00, rate: 0.35}
  - {up_to: 99999999.00, rate: 0.37}
safe_harbor_pct: 1.10
estimated_tax_due_dates:
  - {quarter: 1, due_date: "2026-04-15"}
  - {quarter: 2, due_date: "2026-06-15"}
  - {quarter: 3, due_date: "2026-09-15"}
  - {quarter: 4, due_date: "2027-01-15"}
"""

_PROFILE_YAML = """\
tax_year: 2026
filing_status: mfj
w2:
  - {employer: "Acme", ytd_wages: 60000.00, ytd_federal_withholding: 8000.00, ytd_ss_wages: 60000.00}
expected_investment_income: {interest: 500.00, dividends_qualified: 200.00, capital_gains_lt: 300.00}
prior_year_total_tax: 20000.00
estimated_payments:
  - {date: "2026-04-15", amount: 4000.00}
"""


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    (d / "tax_tables").mkdir(parents=True)
    (d / "tax_tables" / "2026.yaml").write_text(_TABLES_YAML)
    return d


@pytest.fixture()
def config_dir_with_profile(config_dir: Path) -> Path:
    (config_dir / "tax_profile.yaml").write_text(_PROFILE_YAML)
    return config_dir


def _det_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))


def _tx(
    session: Session,
    *,
    amount: str,
    direction: str,
    entity: str,
    date: str,
    tax_category: str,
    description: str = "Test",
    status: str = TransactionStatus.CONFIRMED.value,
    reimbursement_link: str | None = None,
) -> Transaction:
    key = "|".join((entity, direction, date, tax_category, description, amount))
    tx = Transaction(
        id=_det_id("tx", key),
        source=Source.GMAIL_N8N.value,
        source_id=_det_id("src", key),
        source_hash=_det_id("hash", key),
        date=date,
        description=description,
        amount=Decimal(amount),
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=tax_category,
        status=status,
        confidence=0.9,
        raw_data={},
        confirmed_by=ConfirmedBy.AUTO.value,
        reimbursement_link=reimbursement_link,
    )
    session.add(tx)
    session.commit()
    return tx


def _seed_year(session: Session) -> None:
    # Sparkry: $5,000/mo consulting income + $1,000/mo supplies, Jan-Jun 2026.
    for month in range(1, 7):
        d = f"2026-{month:02d}-10"
        _tx(session, amount="5000.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value, date=d, tax_category=TaxCategory.CONSULTING_INCOME.value)
        _tx(session, amount="-1000.00", direction=Direction.EXPENSE.value, entity=Entity.SPARKRY.value, date=d, tax_category=TaxCategory.SUPPLIES.value)
    # BlackLine: $2,000/mo sales income + $500/mo office expense, Jan-Jun 2026.
    for month in range(1, 7):
        d = f"2026-{month:02d}-15"
        _tx(session, amount="2000.00", direction=Direction.INCOME.value, entity=Entity.BLACKLINE.value, date=d, tax_category=TaxCategory.SALES_INCOME.value)
        _tx(session, amount="-500.00", direction=Direction.EXPENSE.value, entity=Entity.BLACKLINE.value, date=d, tax_category=TaxCategory.OFFICE_EXPENSE.value)
    # Prior/next year data must not leak in.
    _tx(session, amount="99999.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value, date="2025-12-31", tax_category=TaxCategory.CONSULTING_INCOME.value)
    _tx(session, amount="99999.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value, date="2027-01-01", tax_category=TaxCategory.CONSULTING_INCOME.value)
    # Rejected row must not count.
    _tx(session, amount="-500.00", direction=Direction.EXPENSE.value, entity=Entity.SPARKRY.value, date="2026-03-01", tax_category=TaxCategory.SUPPLIES.value, status=TransactionStatus.REJECTED.value)


class TestPeriodAnchor:
    def test_january_labels_prior_year_q4(self) -> None:
        occ, label = txf.period_anchor(date(2026, 1, 15))
        assert occ == "2026-01-01"
        assert label == "2025-Q4"

    def test_april_labels_q1(self) -> None:
        occ, label = txf.period_anchor(date(2026, 4, 20))
        assert occ == "2026-04-01"
        assert label == "2026-Q1"

    def test_june_labels_q2(self) -> None:
        occ, label = txf.period_anchor(date(2026, 7, 7))
        assert occ == "2026-06-01"
        assert label == "2026-Q2"

    def test_september_labels_q3(self) -> None:
        occ, label = txf.period_anchor(date(2026, 9, 20))
        assert occ == "2026-09-01"
        assert label == "2026-Q3"

    def test_december_off_cycle_snaps_to_september(self) -> None:
        occ, label = txf.period_anchor(date(2026, 12, 31))
        assert occ == "2026-09-01"
        assert label == "2026-Q3"


class TestLinearProject:
    def test_basic_annualization(self) -> None:
        assert txf._linear_project(Decimal("100"), 365, 100) == Decimal("365")

    def test_zero_days_elapsed_returns_zero(self) -> None:
        assert txf._linear_project(Decimal("100"), 365, 0) == Decimal("0")


class TestSeasonalityGuard:
    def test_trips_on_single_month_concentration(self) -> None:
        guard, reason = txf._seasonality_guard({1: Decimal("1000"), 2: Decimal("1000"), 3: Decimal("8000")}, Decimal("10000"), 100)
        assert guard is True
        assert "80.0%" in (reason or "")

    def test_trips_on_short_ytd_span(self) -> None:
        guard, reason = txf._seasonality_guard({1: Decimal("100")}, Decimal("100"), 30)
        assert guard is True
        assert "30d" in (reason or "")

    def test_does_not_trip_on_even_distribution(self) -> None:
        guard, _ = txf._seasonality_guard({1: Decimal("2500"), 2: Decimal("2500"), 3: Decimal("2500"), 4: Decimal("2500")}, Decimal("10000"), 120)
        assert guard is False

    def test_boundary_exactly_40_pct_does_not_trip(self) -> None:
        guard, _ = txf._seasonality_guard({1: Decimal("4000"), 2: Decimal("4000"), 3: Decimal("2000")}, Decimal("10000"), 100)
        assert guard is False


class TestSETax:
    def test_hand_verified_amounts(self) -> None:
        cfg = {
            "se_tax_rate": Decimal("0.9235"),
            "ss_rate": Decimal("0.124"),
            "medicare_rate": Decimal("0.029"),
            "addl_medicare_rate": Decimal("0.009"),
            "ss_wage_base": Decimal("176100.00"),
            "addl_medicare_mfj_threshold": Decimal("250000.00"),
        }
        result = txf._compute_se_tax(Decimal("100000"), Decimal("0"), Decimal("0"), cfg)
        assert result["se_base"] == Decimal("92350.00")
        assert result["ss_tax"] == Decimal("11451.40")
        assert result["medicare_tax"] == Decimal("2678.15")
        assert result["addl_medicare_tax"] == Decimal("0.00")
        assert result["total"] == Decimal("14129.55")
        assert result["half_deduction"] == Decimal("7064.78")

    def test_addl_medicare_kicks_in_above_threshold(self) -> None:
        cfg = {
            "se_tax_rate": Decimal("1"),
            "ss_rate": Decimal("0"),
            "medicare_rate": Decimal("0"),
            "addl_medicare_rate": Decimal("0.009"),
            "ss_wage_base": Decimal("0"),
            "addl_medicare_mfj_threshold": Decimal("250000.00"),
        }
        result = txf._compute_se_tax(Decimal("300000"), Decimal("0"), Decimal("0"), cfg)
        # se_base=300000; combined=300000; over=50000; addl=50000*0.009=450
        assert result["addl_medicare_tax"] == Decimal("450.00")

    def test_addl_medicare_uses_uncapped_medicare_wages_not_capped_ss_wages(self) -> None:
        """P3-d4f regression: W-2 SS wages capped at ss_wage_base understate
        combined income for the Additional Medicare threshold test when the
        household has W-2 wages above the SS cap; Medicare wages are
        uncapped and must be used for that test instead."""
        cfg = {
            "se_tax_rate": Decimal("1"),
            "ss_rate": Decimal("0"),
            "medicare_rate": Decimal("0"),
            "addl_medicare_rate": Decimal("0.009"),
            "ss_wage_base": Decimal("176100.00"),
            "addl_medicare_mfj_threshold": Decimal("250000.00"),
        }
        # W-2 SS wages capped at 176100; uncapped Medicare wages are 260000.
        # se_base=40000. Using capped SS wages: combined=216100, under
        # threshold, addl=0. Using uncapped Medicare wages: combined=300000,
        # over=50000, addl=40000*0.009=360 (capped at se_base=40000).
        result = txf._compute_se_tax(Decimal("40000"), Decimal("176100.00"), Decimal("260000.00"), cfg)
        assert result["addl_medicare_tax"] == Decimal("360.00")


class TestQBI:
    def test_under_threshold_no_phaseout(self) -> None:
        cfg = {"qbi_rate": Decimal("0.20"), "qbi_mfj_threshold": Decimal("383900.00"), "qbi_mfj_phase_out_width": Decimal("100000.00")}
        result = txf._compute_qbi(Decimal("100000"), Decimal("200000"), Decimal("0"), cfg, True)
        assert result["before_phaseout"] == Decimal("20000.00")
        assert result["phase_out_fraction"] == Decimal("0.00")
        assert result["final"] == Decimal("20000.00")

    def test_partial_phaseout_at_midband(self) -> None:
        cfg = {"qbi_rate": Decimal("0.20"), "qbi_mfj_threshold": Decimal("383900.00"), "qbi_mfj_phase_out_width": Decimal("100000.00")}
        result = txf._compute_qbi(Decimal("100000"), Decimal("433900"), Decimal("0"), cfg, True)
        assert result["phase_out_fraction"] == Decimal("0.50")
        assert result["final"] == Decimal("10000.00")

    def test_fully_phased_out_past_band(self) -> None:
        cfg = {"qbi_rate": Decimal("0.20"), "qbi_mfj_threshold": Decimal("383900.00"), "qbi_mfj_phase_out_width": Decimal("100000.00")}
        result = txf._compute_qbi(Decimal("100000"), Decimal("500000"), Decimal("0"), cfg, True)
        assert result["phase_out_fraction"] == Decimal("1.00")
        assert result["final"] == Decimal("0.00")

    def test_non_sstb_never_phases_out(self) -> None:
        cfg = {"qbi_rate": Decimal("0.20"), "qbi_mfj_threshold": Decimal("383900.00"), "qbi_mfj_phase_out_width": Decimal("100000.00")}
        result = txf._compute_qbi(Decimal("100000"), Decimal("900000"), Decimal("0"), cfg, False)
        assert result["phase_out_fraction"] == Decimal("0.00")
        assert result["final"] == Decimal("20000.00")


class TestBracketWalk:
    BRACKETS = [
        {"up_to": Decimal("23850.00"), "rate": Decimal("0.10")},
        {"up_to": Decimal("96950.00"), "rate": Decimal("0.12")},
        {"up_to": Decimal("206700.00"), "rate": Decimal("0.22")},
    ]

    def test_hand_verified_mid_bracket(self) -> None:
        result = txf._walk_brackets(Decimal("50000"), self.BRACKETS)
        assert result["total_tax"] == Decimal("5523.00")
        assert result["marginal_rate"] == Decimal("0.12")
        assert result["distance_to_next_edge"] == Decimal("46950.00")
        assert result["effective_rate"] == Decimal("0.1105")

    def test_zero_taxable_income(self) -> None:
        result = txf._walk_brackets(Decimal("0"), self.BRACKETS)
        assert result["total_tax"] == Decimal("0.00")
        assert result["effective_rate"] is None


class TestLTCGStacked:
    """P2-c3e: net LTCG/qualified dividends must be taxed at preferential
    rates stacked on top of ordinary income, not folded into the ordinary
    MFJ brackets."""

    LTCG_BRACKETS = [
        {"up_to": Decimal("96700.00"), "rate": Decimal("0.00")},
        {"up_to": Decimal("600050.00"), "rate": Decimal("0.15")},
        {"up_to": Decimal("99999999.00"), "rate": Decimal("0.20")},
    ]

    def test_ltcg_entirely_in_zero_pct_band(self) -> None:
        # Ordinary income 50000, LTCG 20000 -> stack occupies [50000,70000],
        # entirely under the 96700 0%-rate ceiling.
        tax = txf._tax_ltcg_stacked(Decimal("50000"), Decimal("20000"), self.LTCG_BRACKETS)
        assert tax == Decimal("0")

    def test_ltcg_straddles_zero_and_15_pct_bands(self) -> None:
        # Ordinary income 90000, LTCG 20000 -> stack occupies [90000,110000].
        # 6700 in the 0% band (90000..96700) + 13300 in the 15% band
        # (96700..110000) = 13300 * 0.15 = 1995.00.
        tax = txf._tax_ltcg_stacked(Decimal("90000"), Decimal("20000"), self.LTCG_BRACKETS)
        assert tax == Decimal("1995.00")

    def test_zero_ltcg_returns_zero(self) -> None:
        assert txf._tax_ltcg_stacked(Decimal("50000"), Decimal("0"), self.LTCG_BRACKETS) == Decimal("0")

    def test_combine_bracket_with_ltcg_totals_correctly(self) -> None:
        ordinary = txf._walk_brackets(Decimal("90000"), TestBracketWalk.BRACKETS)
        ltcg_tax = txf._tax_ltcg_stacked(Decimal("90000"), Decimal("20000"), self.LTCG_BRACKETS)
        combined = txf._combine_bracket_with_ltcg(ordinary, Decimal("110000"), Decimal("90000"), Decimal("20000"), ltcg_tax)
        assert combined["taxable_income"] == Decimal("110000.00")
        assert combined["ordinary_tax"] == ordinary["total_tax"]
        assert combined["ltcg_tax"] == Decimal("1995.00")
        assert combined["total_tax"] == ordinary["total_tax"] + Decimal("1995.00")
        assert combined["marginal_rate"] == ordinary["marginal_rate"]


class TestSafeHarbor:
    def test_hand_verified_remaining_quarters(self) -> None:
        """P2-b2d: each line is the standalone INCREMENTAL amount for that
        due date (not a cumulative running total) — summing all lines must
        equal target - paid_to_date exactly, so an owner acting on each
        line as an independent action reaches (not overshoots) the target."""
        due_dates = [
            {"quarter": 1, "due_date": "2026-04-15"},
            {"quarter": 2, "due_date": "2026-06-15"},
            {"quarter": 3, "due_date": "2026-09-15"},
            {"quarter": 4, "due_date": "2027-01-15"},
        ]
        result = txf._compute_safe_harbor(
            Decimal("20000.00"), Decimal("8000.00"),
            [{"date": "2026-04-15", "amount": "4000.00"}],
            due_dates, Decimal("1.10"), date(2026, 7, 7),
        )
        assert result["target"] == Decimal("22000.00")
        assert result["paid_to_date"] == Decimal("12000.00")
        # Q3 cumulative target = 22000*3/4=16500; increment over paid_to_date
        # (12000) = 4500. Q4 cumulative target = 22000; increment over Q3's
        # cumulative target (16500) = 5500. 4500 + 5500 = 10000 = the total
        # remaining shortfall (22000 - 12000) — standalone-additive.
        assert result["lines"] == [
            {"due_date": "2026-09-15", "set_aside": Decimal("4500")},
            {"due_date": "2027-01-15", "set_aside": Decimal("5500")},
        ]
        assert sum((line["set_aside"] for line in result["lines"]), Decimal("0")) == result["target"] - result["paid_to_date"]

    def test_rounding_boundary_lines_sum_to_rounded_shortfall(self) -> None:
        """P1-shr1: increments landing exactly on $.50 must NOT compound
        upward (independent ROUND_HALF_UP per line gave 4×$3 = $12 against a
        $10 shortfall). Carry-the-remainder rounding: printed lines sum to
        the rounded remaining shortfall."""
        due_dates = [
            {"quarter": 1, "due_date": "2026-04-15"},
            {"quarter": 2, "due_date": "2026-06-15"},
            {"quarter": 3, "due_date": "2026-09-15"},
            {"quarter": 4, "due_date": "2027-01-15"},
        ]
        # target = 10.00 → per-quarter cumulative 2.50/5.00/7.50/10.00; every
        # increment is exactly $2.50 — the worst case for per-line HALF_UP.
        result = txf._compute_safe_harbor(
            Decimal("10.00") / Decimal("1.10"), Decimal("0.00"), [],
            due_dates, Decimal("1.10"), date(2026, 1, 1),
        )
        lines = result["lines"]
        assert len(lines) == 4
        total = sum((line["set_aside"] for line in lines), Decimal("0"))
        # Rounded shortfall is $10 — not $12.
        assert total == Decimal("10")
        # Cumulative never regresses and each line is a whole-dollar amount.
        assert all(line["set_aside"] == line["set_aside"].to_integral_value() for line in lines)
        assert all(line["set_aside"] >= 0 for line in lines)


class TestComputeTXF:
    def test_business_only_when_profile_missing(self, session: Session, config_dir: Path) -> None:
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        assert data["business_only"] is True
        assert "not found" in (data["business_only_reason"] or "")
        assert data["bracket"]["available"] is False
        assert data["safe_harbor"]["available"] is False
        # Business projections still render fully.
        assert data["sparkry"]["gross_receipts_ytd"] == Decimal("30000.00")

    def test_business_only_when_prior_year_tax_zero(self, session: Session, config_dir: Path) -> None:
        (config_dir / "tax_profile.yaml").write_text(_PROFILE_YAML.replace("prior_year_total_tax: 20000.00", "prior_year_total_tax: 0.00"))
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        assert data["business_only"] is True
        assert "prior_year_total_tax" in (data["business_only_reason"] or "")

    def test_business_only_when_profile_unparseable(
        self, session: Session, config_dir: Path
    ) -> None:
        """P1-cfg9a / REQ-TXF-003: a YAML typo in the hand-edited profile
        degrades to business-only mode with a banner — never crashes the
        whole quarterly report."""
        (config_dir / "tax_profile.yaml").write_text(
            "prior_year_total_tax: 20000.00\n  bad_indent: : nope\n"
        )
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        assert data["business_only"] is True
        assert "not valid YAML" in (data["business_only_reason"] or "")
        # Business projections still render fully.
        assert data["sparkry"]["gross_receipts_ytd"] == Decimal("30000.00")

    def test_full_household_mode_with_profile(self, session: Session, config_dir_with_profile: Path) -> None:
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir_with_profile)
        assert data["business_only"] is False
        assert data["bracket"]["available"] is True
        assert data["safe_harbor"]["available"] is True
        assert data["safe_harbor"]["target"] == Decimal("22000.00")

    def test_prior_and_next_year_excluded(self, session: Session, config_dir: Path) -> None:
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        # 30000 YTD, not 129999 (which would include the leaked 99999 rows).
        assert data["sparkry"]["gross_receipts_ytd"] == Decimal("30000.00")

    def test_rejected_excluded(self, session: Session, config_dir: Path) -> None:
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        assert data["sparkry"]["expenses_ytd"] == Decimal("6000.00")  # not 6500

    def test_linked_reimbursement_receipt_excluded_from_gross_receipts(
        self, session: Session, config_dir: Path
    ) -> None:
        """P1-txfr3imb regression: a reimbursement receipt classified into
        an INCOME_CATEGORIES tax_category (plausible if the classifier tags
        a Stripe/bank deposit as CONSULTING_INCOME before it is manually
        linked via link_reimbursement) must NOT inflate gross_receipts_ytd —
        matching compute_entity_pl's explicit exclusion of
        reimbursement_target_ids from revenue. The linked reimbursable leg
        must likewise not double as a deductible expense."""
        _seed_year(session)
        baseline = txf.compute_txf(session, TODAY, config_dir=config_dir)
        baseline_gross = baseline["sparkry"]["gross_receipts_ytd"]
        baseline_expenses = baseline["sparkry"]["expenses_ytd"]

        income_tx = _tx(
            session, amount="1200.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
            date="2026-06-20", tax_category=TaxCategory.CONSULTING_INCOME.value,
            description="Cardinal Health reimbursement receipt",
        )
        expense_tx = _tx(
            session, amount="-1200.00", direction=Direction.REIMBURSABLE.value, entity=Entity.SPARKRY.value,
            date="2026-06-19", tax_category=TaxCategory.SUPPLIES.value,
            description="Cardinal Health reimbursable expense",
            reimbursement_link=income_tx.id,
        )
        # Mirror link_reimbursement's bidirectional link (transactions.py):
        # the income leg also points back at the expense leg.
        income_tx.reimbursement_link = expense_tx.id
        session.add(income_tx)
        session.commit()

        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        assert data["sparkry"]["gross_receipts_ytd"] == baseline_gross
        assert data["sparkry"]["expenses_ytd"] == baseline_expenses

    def test_one_reimbursement_linked_to_many_expenses_excludes_all_legs(
        self, session: Session, config_dir: Path
    ) -> None:
        """Issue #62: link_reimbursement has no 1:1 enforcement — one
        reimbursement income row can be linked to several expense legs (one
        deposit covering multiple trip expenses). ``_exclude_reimbursement_pairs``
        must exclude EVERY linked expense leg, not just the last one linked
        (whose id happens to land in the income row's single-valued
        back-pointer)."""
        _seed_year(session)
        baseline = txf.compute_txf(session, TODAY, config_dir=config_dir)
        baseline_gross = baseline["sparkry"]["gross_receipts_ytd"]
        baseline_expenses = baseline["sparkry"]["expenses_ytd"]

        income_tx = _tx(
            session, amount="1446.18", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
            date="2026-06-20", tax_category=TaxCategory.CONSULTING_INCOME.value,
            description="Cardinal Health trip reimbursement",
        )
        expense_a = _tx(
            session, amount="-900.00", direction=Direction.REIMBURSABLE.value, entity=Entity.SPARKRY.value,
            date="2026-06-18", tax_category=TaxCategory.SUPPLIES.value,
            description="Flight", reimbursement_link=income_tx.id,
        )
        expense_b = _tx(
            session, amount="-546.18", direction=Direction.REIMBURSABLE.value, entity=Entity.SPARKRY.value,
            date="2026-06-19", tax_category=TaxCategory.SUPPLIES.value,
            description="Hotel", reimbursement_link=income_tx.id,
        )
        # The income row's back-pointer is single-valued, so the second
        # link_reimbursement call overwrites it — it can only ever
        # remember the last-linked expense.
        income_tx.reimbursement_link = expense_a.id
        session.add(income_tx)
        session.commit()
        income_tx.reimbursement_link = expense_b.id
        session.add(income_tx)
        session.commit()

        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        assert data["sparkry"]["gross_receipts_ytd"] == baseline_gross
        assert data["sparkry"]["expenses_ytd"] == baseline_expenses

    def test_bno_rows_use_bno_tax_rates(self, session: Session, config_dir: Path) -> None:
        from src.export.bno_tax import BO_RATE

        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        sparkry_row = next(r for r in data["bno_rows"] if r["entity"] == Entity.SPARKRY.value)
        assert sparkry_row["rate"] == BO_RATE["ServiceOther"]
        assert sparkry_row["cadence"] == "monthly"
        blackline_row = next(r for r in data["bno_rows"] if r["entity"] == Entity.BLACKLINE.value)
        assert blackline_row["rate"] == BO_RATE["Retailing"]
        assert blackline_row["cadence"] == "quarterly"

    def test_bno_accrual_excludes_reimbursement_pair(self, session: Session, config_dir: Path) -> None:
        """REQ-TXF-001 / P2-bno2 regression: the WA B&O accrual
        (``_project_bno``) must exclude both legs of a linked reimbursement
        pair from projected gross receipts, exactly like the Schedule C
        gross-receipts figure already does (P1-txfr3imb) — otherwise a
        Cardinal-Health-style reimbursement receipt classified into an
        INCOME_CATEGORIES tax_category would correctly stay out of Schedule
        C but still silently inflate the B&O accrual base."""
        _seed_year(session)
        baseline = txf.compute_txf(session, TODAY, config_dir=config_dir)
        baseline_bno = next(r for r in baseline["bno_rows"] if r["entity"] == Entity.SPARKRY.value)

        income_tx = _tx(
            session, amount="1200.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
            date="2026-06-20", tax_category=TaxCategory.CONSULTING_INCOME.value,
            description="Cardinal Health reimbursement receipt",
        )
        expense_tx = _tx(
            session, amount="-1200.00", direction=Direction.REIMBURSABLE.value, entity=Entity.SPARKRY.value,
            date="2026-06-19", tax_category=TaxCategory.SUPPLIES.value,
            description="Cardinal Health reimbursable expense",
            reimbursement_link=income_tx.id,
        )
        # Mirror link_reimbursement's bidirectional link (transactions.py).
        income_tx.reimbursement_link = expense_tx.id
        session.add(income_tx)
        session.commit()

        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        sparkry_bno = next(r for r in data["bno_rows"] if r["entity"] == Entity.SPARKRY.value)
        assert sparkry_bno["projected_gross"] == baseline_bno["projected_gross"]
        assert sparkry_bno["annual_tax"] == baseline_bno["annual_tax"]
        assert sparkry_bno["per_period_tax"] == baseline_bno["per_period_tax"]


class TestBusinessOnlyRenderedOutput:
    """REQ-TXF-003 / P2-buo2 regression: exercise the actual renderer
    (``render_report``), not just ``compute_txf``'s ``business_only`` flag —
    the business-only banner must appear and the household bracket/safe-harbor
    sections must be absent from the RENDERED text when tax_profile.yaml is
    missing."""

    def test_business_only_banner_present_and_household_sections_absent(
        self, session: Session, config_dir: Path
    ) -> None:
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        assert data["business_only"] is True
        rendered = txf.render_report(data)

        assert "⚠️ BUSINESS-ONLY MODE" in rendered
        assert "tax_profile.yaml not found" in rendered

        # Both household-dependent sections must print the UNAVAILABLE
        # placeholder, not a computed bracket/safe-harbor breakdown.
        assert rendered.count("UNAVAILABLE — fill config/tax_profile.yaml") == 2
        assert "marginal rate:" not in rendered
        assert "distance to next bracket edge:" not in rendered
        assert "target:" not in rendered
        assert "Set aside $" not in rendered

        # Business-only projections still render fully.
        assert "SCHEDULE C (Sparkry)" in rendered
        assert "WA B&O ACCRUAL" in rendered


class TestSeasonalityGuardRenderedEndToEnd:
    """REQ-TXF-001 / P2-seas2 regression: end-to-end seasonality-guard —
    seed a year where one month holds >40% of YTD gross receipts, run
    ``compute_txf`` + ``render_report`` (not the guard helper in isolation),
    and assert the trailing-3-month alternative line and the ASSUMPTIONS
    block's HIGH VARIANCE banner both make it into the actual rendered
    text."""

    def test_concentrated_month_trips_guard_and_renders_alt_and_assumptions(
        self, session: Session, config_dir: Path
    ) -> None:
        # 5 months at $1,000 + June at $20,000 -> June holds 20000/25000 =
        # 80% of YTD gross receipts, well over the 40% trip threshold.
        for month in range(1, 6):
            d = f"2026-{month:02d}-10"
            _tx(
                session, amount="1000.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
                date=d, tax_category=TaxCategory.CONSULTING_INCOME.value,
            )
        _tx(
            session, amount="20000.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
            date="2026-06-10", tax_category=TaxCategory.CONSULTING_INCOME.value,
        )

        data = txf.compute_txf(session, TODAY, config_dir=config_dir)
        assert data["seasonality_guard"] is True
        assert data["sparkry"]["gross_receipts_projected_alt"] is not None

        rendered = txf.render_report(data)
        assert "ASSUMPTIONS" in rendered
        assert "⚠️ HIGH VARIANCE" in rendered
        assert "trailing-3-month alternative shown alongside gross receipts" in rendered
        assert "alt trailing-3mo:" in rendered


class TestDeterminism:
    def test_two_runs_identical(self, session: Session, config_dir_with_profile: Path) -> None:
        _seed_year(session)
        d1 = txf.compute_txf(session, TODAY, config_dir=config_dir_with_profile)
        d2 = txf.compute_txf(session, TODAY, config_dir=config_dir_with_profile)
        assert txf.render_report(d1) == txf.render_report(d2)


_PATH_LINE_RE = re.compile(r"^  (tax_tables|tax_profile): .*$", re.MULTILINE)


def _normalize_golden(text: str) -> str:
    """Blank the tmp_path/mtime lines — inherently volatile across pytest
    invocations (fresh tmp_path each run) and environments, unrelated to the
    report's actual computed content."""
    return _PATH_LINE_RE.sub(lambda m: f"  {m.group(1)}: <normalized for golden compare>", text)


class TestGoldenOutput:
    def test_render_matches_golden_fixture(self, session: Session, config_dir_with_profile: Path) -> None:
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir_with_profile)
        rendered = _normalize_golden(txf.render_report(data))
        if not GOLDEN_PATH.exists():
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN_PATH.write_text(rendered)
            pytest.fail(f"Golden fixture didn't exist; wrote it to {GOLDEN_PATH}. Re-run to verify.")
        assert rendered == GOLDEN_PATH.read_text()


class TestDispatchLedger:
    def test_apply_writes_resend_email_channel(self, session: Session, config_dir_with_profile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESEND_API_KEY", "test-key")

        def _fake_send(params: object) -> dict[str, str]:
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir_with_profile)
        body = txf.render_report(data)
        subject = txf.build_subject(data)
        occ, label = txf.period_anchor(TODAY)
        result = report_email.dispatch_report(
            session, alert_key=f"txf:{label}", occurrence_date=occ,
            alert_type="tax_forecast", entity="all", subject=subject, body=body, apply=True,
        )
        assert result.status == "sent"
        row = session.query(AlertDispatch).filter_by(alert_key=f"txf:{label}", occurrence_date=occ).one()
        assert row.delivery_channel == "resend_email"
        assert row.payload_json is None

    def test_catchup_dedup(self, session: Session, config_dir_with_profile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        calls: list[object] = []

        def _fake_send(params: object) -> dict[str, str]:
            calls.append(params)
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)
        _seed_year(session)
        data = txf.compute_txf(session, TODAY, config_dir=config_dir_with_profile)
        body = txf.render_report(data)
        subject = txf.build_subject(data)
        occ, label = txf.period_anchor(TODAY)

        def _dispatch() -> report_email.SendResult:
            return report_email.dispatch_report(
                session, alert_key=f"txf:{label}", occurrence_date=occ,
                alert_type="tax_forecast", entity="all", subject=subject, body=body, apply=True,
            )

        first = _dispatch()
        second = _dispatch()
        assert first.status == "sent"
        assert second.status == "skipped"
        assert len(calls) == 1


class TestCLI:
    def test_dry_run_smoke_exits_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        s = SessionLocal()
        _seed_year(s)
        s.close()

        rc = txf.main(["--date", "2026-07-07", "--db", str(db_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "TAX FORECAST" in captured.out

        # No TXF ledger row written in dry-run
        s2 = SessionLocal()
        assert s2.query(AlertDispatch).filter(AlertDispatch.alert_key.like("txf:%")).count() == 0
        s2.close()
