"""Tests for src/reports/sellability.py — Sellability metrics.

REQ-ID: REQ-SEL-001..002.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import resend as _resend
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401 — registers all ORM models before create_all
from src.alerts.models import AlertDispatch
from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import (
    AccountType,
    Broker,
    ConfirmedBy,
    Direction,
    Entity,
    Source,
    TaxCategory,
    TransactionStatus,
)
from src.models.invoice import Customer, Invoice
from src.models.transaction import Transaction
from src.reports import report_email
from src.reports import sellability as sel

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "reports-golden" / "sel-basic.txt"
TODAY = date(2026, 7, 1)  # scope = June 2026


def _det_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionLocal()
    yield s
    s.close()


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
    source: str = Source.GMAIL_N8N.value,
    raw_data: dict[str, Any] | None = None,
    reimbursement_link: str | None = None,
) -> Transaction:
    key = "|".join((entity, direction, date, tax_category, description, amount, source))
    tx = Transaction(
        id=_det_id("tx", key),
        source=source,
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
        raw_data=raw_data or {},
        confirmed_by=ConfirmedBy.AUTO.value,
        reimbursement_link=reimbursement_link,
    )
    session.add(tx)
    session.commit()
    return tx


def _customer(session: Session, name: str, billing_model: str, calendar_patterns: dict[str, Any] | None = None) -> Customer:
    cust = Customer(
        id=_det_id("cust", name),
        name=name,
        billing_model=billing_model,
        invoice_prefix=name[:2].upper(),
        calendar_patterns=calendar_patterns,
    )
    session.add(cust)
    session.commit()
    return cust


def _paid_invoice(session: Session, customer: Customer, txn: Transaction, amount: str) -> Invoice:
    inv = Invoice(
        id=_det_id("inv", customer.id, txn.id),
        invoice_number=f"INV-{txn.date}-{customer.invoice_prefix}",
        customer_id=customer.id,
        entity=Entity.SPARKRY.value,
        subtotal=Decimal(amount),
        total=Decimal(amount),
        status="paid",
        payment_transaction_id=txn.id,
    )
    session.add(inv)
    session.commit()
    return inv


def _full_fixture(session: Session) -> None:
    acme = _customer(session, "Acme Corp", "flat_rate")
    bolt = _customer(session, "Bolt LLC", "hourly", calendar_patterns=None)

    for month in range(1, 7):
        d = f"2026-{month:02d}-05"
        txn = _tx(
            session, amount="4000.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
            date=d, tax_category=TaxCategory.CONSULTING_INCOME.value, description="Acme retainer",
        )
        _paid_invoice(session, acme, txn, "4000.00")
        _tx(session, amount="-200.00", direction=Direction.EXPENSE.value, entity=Entity.SPARKRY.value, date=d, tax_category=TaxCategory.SUPPLIES.value)
        _tx(session, amount="-150.00", direction=Direction.EXPENSE.value, entity=Entity.SPARKRY.value, date=d, tax_category=TaxCategory.HEALTH_INSURANCE.value, description="health premium")

    bolt_txn = _tx(
        session, amount="1000.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
        date="2026-03-10", tax_category=TaxCategory.CONSULTING_INCOME.value, description="Bolt project",
    )
    _paid_invoice(session, bolt, bolt_txn, "1000.00")

    # Stripe non-invoice income, mapped via stripe_client_map desc_contains rule.
    _tx(
        session, amount="500.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
        date="2026-02-15", tax_category=TaxCategory.SUBSCRIPTION_INCOME.value, description="Substack payment",
        source=Source.STRIPE.value,
    )
    # Unattributed stripe income — no invoice, no map rule match.
    _tx(
        session, amount="300.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
        date="2026-04-20", tax_category=TaxCategory.CONSULTING_INCOME.value, description="misc income",
        source=Source.STRIPE.value,
    )

    # Rejected row must not count.
    _tx(
        session, amount="9999.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
        date="2026-06-01", tax_category=TaxCategory.CONSULTING_INCOME.value, status=TransactionStatus.REJECTED.value,
    )

    # BlackLine burn.
    _tx(session, amount="1200.00", direction=Direction.INCOME.value, entity=Entity.BLACKLINE.value, date="2026-06-10", tax_category=TaxCategory.SALES_INCOME.value)
    _tx(session, amount="-2500.00", direction=Direction.EXPENSE.value, entity=Entity.BLACKLINE.value, date="2026-06-12", tax_category=TaxCategory.OFFICE_EXPENSE.value)

    # Unnamed Fidelity TOD account (REQ-FIX-DAT-002).
    session.add(
        Account(
            id=_det_id("acct", "fidelity-tod"),
            broker=Broker.FIDELITY.value,
            account_number="Z99999999",
            account_name=None,
            account_type=AccountType.TOD.value,
            entity=Entity.PERSONAL.value,
        )
    )
    session.commit()


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "sellability.yaml").write_text(
        "addback_categories: [HEALTH_INSURANCE]\n"
        "owner_salary_monthly: 0.00\n"
        "one_time_items: []\n"
        "recurring_customers: {}\n"
        'stripe_client_map:\n  - {match: "desc_contains:substack", client: "Substack"}\n'
    )
    return d


@pytest.fixture()
def config_dir_recurring_override(tmp_path: Path) -> Path:
    """P2-selrec2 fixture: overrides ``recurring_customers`` to force Bolt
    LLC (hourly billing, no calendar_patterns -> not recurring by default)
    into the recurring bucket, and adds a ``stripe_client_map`` desc_contains
    rule that attributes the previously-UNATTRIBUTED "misc income" Stripe
    row to a named client."""
    d = tmp_path / "config_override"
    d.mkdir()
    (d / "sellability.yaml").write_text(
        "addback_categories: [HEALTH_INSURANCE]\n"
        "owner_salary_monthly: 0.00\n"
        "one_time_items: []\n"
        'recurring_customers: {"Bolt LLC": true}\n'
        "stripe_client_map:\n"
        '  - {match: "desc_contains:substack", client: "Substack"}\n'
        '  - {match: "desc_contains:misc", client: "Foo Corp"}\n'
    )
    return d


class TestScopeMonth:
    def test_july_1st_scopes_to_june(self) -> None:
        start, end = sel.scope_month(date(2026, 7, 1))
        assert start == date(2026, 6, 1)
        assert end == date(2026, 7, 1)

    def test_january_1st_scopes_to_prior_december(self) -> None:
        start, end = sel.scope_month(date(2026, 1, 1))
        assert start == date(2025, 12, 1)
        assert end == date(2026, 1, 1)

    def test_mid_month_run_still_scopes_to_prior_month(self) -> None:
        start, end = sel.scope_month(date(2026, 7, 15))
        assert start == date(2026, 6, 1)
        assert end == date(2026, 7, 1)


class TestAddMonths:
    def test_forward_wraps_year(self) -> None:
        assert sel._add_months(date(2026, 11, 1), 3) == date(2027, 2, 1)

    def test_backward_wraps_year(self) -> None:
        assert sel._add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)


class TestComputeSellability:
    def test_client_revenue_and_concentration(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Point the module's config loader at the temp config dir.
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        _full_fixture(session)
        data = sel.compute_sellability(session, TODAY)

        clients = {r["client"]: r for r in data["client_rows_ttm"]}
        assert clients["Acme Corp"]["amount"] == Decimal("24000.00")
        assert clients["Acme Corp"]["recurring"] is True
        assert clients["Bolt LLC"]["amount"] == Decimal("1000.00")
        assert clients["Bolt LLC"]["recurring"] is False
        assert clients["Substack"]["amount"] == Decimal("500.00")
        assert clients[sel.UNATTRIBUTED]["amount"] == Decimal("300.00")
        assert clients[sel.UNATTRIBUTED]["unattributed"] is True

        assert data["top1_pct_ttm"] > Decimal("50")
        assert data["concentration_warn"] is not None
        assert data["unattributed_warn"] is None  # 300/25800 ≈ 1.2%, under 10%

        assert data["recurring_revenue_ttm"] == Decimal("24000.00")
        assert data["project_revenue_ttm"] == Decimal("1800.00")

    def test_sde_includes_healthinsurance_addback(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        _full_fixture(session)
        data = sel.compute_sellability(session, TODAY)
        june_sde = data["month_sde"]
        # net = 4000 - 200 - 150 = 3650; addback HEALTH_INSURANCE = 150; SDE = 3800
        assert june_sde["net_income"] == Decimal("3650.00")
        assert any(line["label"] == "HEALTH_INSURANCE" and line["amount"] == Decimal("150.00") for line in june_sde["addback_lines"])
        assert june_sde["sde"] == Decimal("3800.00")

    def test_linked_reimbursable_pair_excluded_from_sde_and_client_revenue(
        self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-r1emb regression: a linked reimbursable expense/income pair
        (the Cardinal-Health-style case) must contribute ZERO to both the
        month SDE net_income (routed via ``compute_entity_pl``) and to
        client-revenue totals (routed via the module's own
        ``reimbursement_target_ids`` exclusion) — guarding the
        explicitly-flagged pl_engine merge risk."""
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        _full_fixture(session)
        baseline = sel.compute_sellability(session, TODAY)
        baseline_net_income = baseline["month_sde"]["net_income"]
        baseline_sde = baseline["month_sde"]["sde"]
        baseline_client_total = sum((r["amount"] for r in baseline["client_rows_ttm"]), Decimal("0"))

        income_tx = _tx(
            session, amount="750.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value,
            date="2026-06-18", tax_category=TaxCategory.CONSULTING_INCOME.value,
            description="Cardinal Health reimbursement receipt",
        )
        _tx(
            session, amount="-750.00", direction=Direction.REIMBURSABLE.value, entity=Entity.SPARKRY.value,
            date="2026-06-17", tax_category=TaxCategory.SUPPLIES.value,
            description="Cardinal Health reimbursable expense",
            reimbursement_link=income_tx.id,
        )

        data = sel.compute_sellability(session, TODAY)
        assert data["month_sde"]["net_income"] == baseline_net_income
        assert data["month_sde"]["sde"] == baseline_sde
        client_total = sum((r["amount"] for r in data["client_rows_ttm"]), Decimal("0"))
        assert client_total == baseline_client_total

    def test_rejected_txn_excluded(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        _full_fixture(session)
        data = sel.compute_sellability(session, TODAY)
        total = sum((r["amount"] for r in data["client_rows_ttm"]), Decimal("0"))
        assert total == Decimal("25800.00")  # not 35799 (which would include the rejected 9999 row)

    def test_blackline_burn(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        _full_fixture(session)
        data = sel.compute_sellability(session, TODAY)
        assert data["blackline"]["net_month"] == Decimal("-1300.00")  # 1200 - 2500

    def test_fidelity_tod_prompt_fires_when_unnamed_account_exists(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        _full_fixture(session)
        data = sel.compute_sellability(session, TODAY)
        assert data["fidelity_tod_prompt"] is True

    def test_fidelity_tod_prompt_absent_when_no_such_account(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        data = sel.compute_sellability(session, TODAY)
        assert data["fidelity_tod_prompt"] is False

    def test_mom_trend_has_six_rows(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        _full_fixture(session)
        data = sel.compute_sellability(session, TODAY)
        assert len(data["mom_trend"]) == 6
        assert data["mom_trend"][-1]["month"] == "2026-06"
        assert data["mom_trend"][0]["month"] == "2026-01"

    def test_config_driven_recurring_override_and_stripe_match_rule_move_attribution(
        self,
        session: Session,
        config_dir: Path,
        config_dir_recurring_override: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REQ-SEL-001..002 / P2-selrec2 regression: ``recurring_customers``
        overrides and ``stripe_client_map`` match rules are config-driven
        (config/sellability.yaml), not hardcoded — changing either in
        config_dir must move the recurring/project revenue split and client
        attribution, not just exercise the config-load smoke path."""
        import src.reports.report_config as rc

        _full_fixture(session)

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        baseline = sel.compute_sellability(session, TODAY)
        baseline_clients = {r["client"]: r for r in baseline["client_rows_ttm"]}
        assert baseline_clients["Bolt LLC"]["recurring"] is False
        assert baseline_clients[sel.UNATTRIBUTED]["amount"] == Decimal("300.00")
        assert baseline["recurring_revenue_ttm"] == Decimal("24000.00")
        assert baseline["project_revenue_ttm"] == Decimal("1800.00")

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir_recurring_override)
        data = sel.compute_sellability(session, TODAY)
        clients = {r["client"]: r for r in data["client_rows_ttm"]}

        # Bolt LLC flips to recurring via the recurring_customers override.
        assert clients["Bolt LLC"]["recurring"] is True
        # The "misc income" row is now attributed to "Foo Corp" via the new
        # stripe_client_map rule, so UNATTRIBUTED no longer appears at all.
        assert sel.UNATTRIBUTED not in clients
        assert clients["Foo Corp"]["amount"] == Decimal("300.00")
        assert clients["Foo Corp"]["unattributed"] is False

        # recurring/project split moves accordingly: Bolt's 1000 shifts from
        # project -> recurring (Acme 24000 + Bolt 1000 = 25000 recurring);
        # project is left with Substack 500 + Foo Corp 300 = 800.
        assert data["recurring_revenue_ttm"] == Decimal("25000.00")
        assert data["project_revenue_ttm"] == Decimal("800.00")


class TestDeterminism:
    def test_two_runs_identical(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        _full_fixture(session)
        d1 = sel.compute_sellability(session, TODAY)
        d2 = sel.compute_sellability(session, TODAY)
        assert sel.render_report(d1) == sel.render_report(d2)


class TestGoldenOutput:
    def test_render_matches_golden_fixture(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        _full_fixture(session)
        data = sel.compute_sellability(session, TODAY)
        rendered = sel.render_report(data)
        if not GOLDEN_PATH.exists():
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN_PATH.write_text(rendered)
            pytest.fail(f"Golden fixture didn't exist; wrote it to {GOLDEN_PATH}. Re-run to verify.")
        assert rendered == GOLDEN_PATH.read_text()


class TestDispatchLedger:
    def test_apply_writes_resend_email_channel(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        monkeypatch.setenv("RESEND_API_KEY", "test-key")

        def _fake_send(params: object) -> dict[str, str]:
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)
        _full_fixture(session)
        data = sel.compute_sellability(session, TODAY)
        body = sel.render_report(data)
        subject = sel.build_subject(data)
        result = report_email.dispatch_report(
            session, alert_key=f"sel:{data['scope_month']}", occurrence_date=data["scope_start"],
            alert_type="sellability_monthly", entity="sparkry", subject=subject, body=body, apply=True,
        )
        assert result.status == "sent"
        row = session.query(AlertDispatch).filter_by(alert_key=f"sel:{data['scope_month']}", occurrence_date=data["scope_start"]).one()
        assert row.delivery_channel == "resend_email"
        assert row.payload_json is None

    def test_catchup_dedup(self, session: Session, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.reports.report_config as rc

        monkeypatch.setattr(rc, "CONFIG_DIR", config_dir)
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        calls: list[object] = []

        def _fake_send(params: object) -> dict[str, str]:
            calls.append(params)
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)
        _full_fixture(session)
        data = sel.compute_sellability(session, TODAY)
        body = sel.render_report(data)
        subject = sel.build_subject(data)

        def _dispatch() -> report_email.SendResult:
            return report_email.dispatch_report(
                session, alert_key=f"sel:{data['scope_month']}", occurrence_date=data["scope_start"],
                alert_type="sellability_monthly", entity="sparkry", subject=subject, body=body, apply=True,
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
        _full_fixture(s)
        s.close()

        rc = sel.main(["--date", "2026-07-01", "--db", str(db_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "SELLABILITY" in captured.out

        # No SEL ledger row written in dry-run
        s2 = SessionLocal()
        assert s2.query(AlertDispatch).filter(AlertDispatch.alert_key.like("sel:%")).count() == 0
        s2.close()
