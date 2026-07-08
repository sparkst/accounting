"""Tests for src/reports/wbr.py — Weekly Business Review scorecard.

REQ-ID: REQ-WBR-001..003.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import resend as _resend
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401 — registers all ORM models before create_all
from src.alerts.models import AlertDispatch
from src.models.audit_event import AuditEvent
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
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem
from src.models.transaction import Transaction
from src.reports import report_email, wbr

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "reports-golden" / "wbr-basic.txt"

TODAY = date(2026, 7, 13)  # a Monday


def _det_id(*parts: str) -> str:
    """Deterministic UUID from content — the golden-fixture test renders a
    txn-id prefix, so fixture IDs must be stable across separate test runs
    (unlike uuid4(), which would silently break the golden comparison every
    other run)."""
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
    description: str = "Test",
    status: str = TransactionStatus.CONFIRMED.value,
    source: str = Source.GMAIL_N8N.value,
    reimbursement_link: str | None = None,
) -> Transaction:
    key = "|".join((source, entity, direction, date, description, amount, status))
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
        tax_category=TaxCategory.CONSULTING_INCOME.value if direction == "income" else TaxCategory.SUPPLIES.value,
        status=status,
        confidence=0.9,
        raw_data={},
        confirmed_by=ConfirmedBy.AUTO.value,
        reimbursement_link=reimbursement_link,
    )
    session.add(tx)
    session.commit()
    return tx


def _seed_week(session: Session, entity: str, revenue: str, expenses: str, week_end: str) -> None:
    """Seed one [week_end - 7, week_end) window. ``week_end`` is the window's
    EXCLUSIVE end (a Monday) — transactions land 3-4 days before it, safely
    inside the half-open window."""
    end = date.fromisoformat(week_end)
    if Decimal(revenue) != 0:
        _tx(session, amount=revenue, direction=Direction.INCOME.value, entity=entity, date=(end - timedelta(days=4)).isoformat())
    if Decimal(expenses) != 0:
        _tx(session, amount=f"-{expenses}", direction=Direction.EXPENSE.value, entity=entity, date=(end - timedelta(days=3)).isoformat())


def _seed_customer_and_invoice(session: Session, *, age_days: int, amount: str) -> Invoice:
    cust = Customer(id=str(uuid.uuid4()), name=f"Client {age_days}", billing_model="flat_rate", invoice_prefix="CL")
    session.add(cust)
    session.flush()
    sent_at = datetime.combine(TODAY - timedelta(days=age_days), datetime.min.time())
    inv = Invoice(
        id=str(uuid.uuid4()),
        invoice_number=f"INV-{age_days}-{uuid.uuid4().hex[:6]}",
        customer_id=cust.id,
        entity=Entity.SPARKRY.value,
        subtotal=Decimal(amount),
        total=Decimal(amount),
        status="sent",
        sent_at=sent_at,
    )
    session.add(inv)
    session.commit()
    return inv


def _seed_account(session: Session, *, broker: str, account_number: str, account_name: str, account_type: str, entity: str) -> Account:
    acct = Account(
        id=str(uuid.uuid4()),
        broker=broker,
        account_number=account_number,
        account_name=account_name,
        account_type=account_type,
        entity=entity,
    )
    session.add(acct)
    session.commit()
    return acct


def _seed_snapshot(session: Session, account: Account, *, balance: str, plaid_type: str, snapshot_date: date) -> None:
    snap = PlaidAccountBalanceSnapshot(
        account_id=account.id,
        snapshot_date=snapshot_date,
        plaid_account_type=plaid_type,
        plaid_account_subtype="checking" if plaid_type == "depository" else None,
        current_balance=Decimal(balance),
        raw_data={},
    )
    session.add(snap)
    session.commit()


def _full_fixture(session: Session) -> None:
    # 6 trend windows, each [end-7, end). The last two are last-wk / this-wk.
    trend_ends = ("2026-06-08", "2026-06-15", "2026-06-22", "2026-06-29", "2026-07-06", "2026-07-13")
    windows = [
        (trend_ends[0], "300.00", "50.00"),
        (trend_ends[1], "310.00", "60.00"),
        (trend_ends[2], "320.00", "70.00"),
        (trend_ends[3], "330.00", "80.00"),
        (trend_ends[4], "8250.00", "800.00"),  # last wk: [06-29, 07-06)
        (trend_ends[5], "8250.00", "550.00"),  # this wk: [07-06, 07-13) (+900 AWS below = 1450 total)
    ]
    for week_end, rev, exp in windows:
        _seed_week(session, Entity.SPARKRY.value, rev, exp, week_end)
    # The named breach cause: a big AWS renewal this week is the single
    # largest expense txn, pushing total expenses to 1450 (vs 800 last wk).
    _tx(
        session,
        amount="-900.00",
        direction=Direction.EXPENSE.value,
        entity=Entity.SPARKRY.value,
        date="2026-07-09",
        description="AWS annual renewal",
    )

    for week_end in trend_ends:
        _seed_week(session, Entity.BLACKLINE.value, "50.00", "20.00", week_end)
        _seed_week(session, Entity.PERSONAL.value, "0.00", "10.00", week_end)

    # A rejected + split_parent row this week — must not affect totals.
    _tx(
        session,
        amount="-9999.00",
        direction=Direction.EXPENSE.value,
        entity=Entity.SPARKRY.value,
        date="2026-07-10",
        status=TransactionStatus.REJECTED.value,
    )

    # AR aging: 4 invoices across the buckets.
    _seed_customer_and_invoice(session, age_days=5, amount="12000.00")   # current
    _seed_customer_and_invoice(session, age_days=20, amount="8000.00")   # 15-30
    _seed_customer_and_invoice(session, age_days=40, amount="7000.00")   # 31-45
    _seed_customer_and_invoice(session, age_days=60, amount="6000.00")   # 45+

    # Cash: Sparkry checking (fine), BlackLine checking (below floor), Amex (liability, fine).
    sparkry_chk = _seed_account(session, broker=Broker.CHASE.value, account_number="1234567891234", account_name="Sparkry Checking", account_type=AccountType.CHECKING.value, entity=Entity.SPARKRY.value)
    blackline_chk = _seed_account(session, broker=Broker.CHASE.value, account_number="9876543215678", account_name="BlackLine Checking", account_type=AccountType.CHECKING.value, entity=Entity.BLACKLINE.value)
    amex = _seed_account(session, broker=Broker.AMEX.value, account_number="1112223330005", account_name="Amex", account_type=AccountType.CREDIT_CARD.value, entity=Entity.SPARKRY.value)

    _seed_snapshot(session, sparkry_chk, balance="18412.00", plaid_type="depository", snapshot_date=date(2026, 7, 12))
    _seed_snapshot(session, blackline_chk, balance="2104.00", plaid_type="depository", snapshot_date=date(2026, 7, 12))
    _seed_snapshot(session, amex, balance="3310.00", plaid_type="credit", snapshot_date=date(2026, 7, 12))

    # Plaid items — both healthy.
    for inst, item_id in (("Chase", "item-chase"), ("Amex", "item-amex")):
        session.add(
            PlaidItem(
                item_id=item_id,
                institution_id=item_id,
                institution_name=inst,
                access_token_encrypted="enc",
                last_sync_at=datetime(2026, 7, 12, 12, 0, 0),
                status="active",
            )
        )
    session.commit()

    # Ops: review queue + auto-confirmed this week.
    for i in range(14):
        _tx(
            session,
            amount="-10.00",
            direction=Direction.EXPENSE.value,
            entity=Entity.SPARKRY.value,
            date="2026-07-07",
            status=TransactionStatus.NEEDS_REVIEW.value,
            description=f"review {i}",
        )
    for _i in range(22):
        session.add(
            AuditEvent(
                entity_id=str(uuid.uuid4()),
                entity_type="account",
                field_changed="status",
                old_value="needs_review",
                new_value="confirmed",
                changed_by="auto:rule:x",
                changed_at=datetime(2026, 7, 8, 10, 0, 0),
            )
        )
    session.commit()

    # Alert ledger: 9 sent, 0 failed in the last 7 days, n8n_webhook channel.
    for i in range(9):
        session.add(
            AlertDispatch(
                alert_key=f"balance:acct{i}:checking:1000",
                occurrence_date="2026-07-12",
                alert_type="balance_milestone",
                entity="sparkry",
                subject="test",
                status="sent",
                delivery_channel="n8n_webhook",
                payload_json="{}",
            )
        )
    session.commit()

    # Freshness: register txns for each source, some stale.
    for src_value, tx_date in (
        (Source.GMAIL_N8N.value, "2026-07-12"),
        (Source.STRIPE.value, "2026-07-13"),
        (Source.PLAID.value, "2026-07-13"),
        (Source.BANK_CSV.value, "2026-07-01"),  # 12d old — within the 35d bank-csv cadence, not stale
    ):
        _tx(session, amount="1.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value, date=tx_date, source=src_value)
    session.commit()


class TestComputeWBR:
    def test_household_combines_all_entities(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        sparkry = next(r for r in data["entities"] if r["entity"] == Entity.SPARKRY.value)
        blackline = next(r for r in data["entities"] if r["entity"] == Entity.BLACKLINE.value)
        personal = next(r for r in data["entities"] if r["entity"] == Entity.PERSONAL.value)
        expected_hh_net_this = (
            sparkry["revenue_this"] - sparkry["expenses_this"]
            + blackline["revenue_this"] - blackline["expenses_this"]
            + personal["revenue_this"] - personal["expenses_this"]
        )
        assert data["household"]["net_this"] == expected_hh_net_this

    def test_sparkry_expenses_breach_names_cause(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        sparkry = next(r for r in data["entities"] if r["entity"] == Entity.SPARKRY.value)
        assert sparkry["expenses_warn"] is not None
        assert any("AWS annual renewal" in w for w in data["warnings_summary"])

    def test_ar_aging_buckets(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        ar = data["ar_aging"]
        assert ar["current"] == Decimal("12000.00")
        assert ar["d15_30"] == Decimal("8000.00")
        assert ar["d31_45"] == Decimal("7000.00")
        assert ar["d45_plus"] == Decimal("6000.00")
        assert ar["total"] == Decimal("33000.00")
        assert ar["warn"] is not None  # 31-45/45+ both > 0

    def test_cash_liability_negated_and_floor_breach_flagged(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        amex = next(c for c in data["cash"] if "Amex" in c["label"])
        assert amex["balance"] == Decimal("-3310.00")
        assert amex["is_liability"] is True
        blackline_chk = next(c for c in data["cash"] if "BlackLine Checking" in c["label"])
        assert blackline_chk["warn"] is not None
        sparkry_chk = next(c for c in data["cash"] if "Sparkry Checking" in c["label"])
        assert sparkry_chk["warn"] is None

    def test_ops_and_delivery_health(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        assert data["ops"]["review_queue"] == 14
        assert data["ops"]["auto_confirmed"] == 22
        assert data["delivery"]["plaid_ok"] == 2
        assert data["delivery"]["plaid_total"] == 2
        assert data["delivery"]["alerts_sent_7d"] == 9
        assert data["delivery"]["alerts_failed_7d"] == 0
        assert data["delivery"]["unmapped_count"] == 0

    def test_rejected_and_split_parent_excluded_from_totals(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        sparkry = next(r for r in data["entities"] if r["entity"] == Entity.SPARKRY.value)
        # The $9,999 rejected expense must not appear in this week's expenses.
        assert sparkry["expenses_this"] < Decimal("9999.00")


class TestFreshnessDeterminism:
    """P1-fr3sh regression: freshness must key off the injected ``today``
    parameter, not real wall-clock ``date.today()``. Pin ``today`` far in the
    past and far in the future (both nowhere near the real test-run date) and
    assert the ``stale`` flag flips purely as a function of the injected date
    — proving the computation cannot be silently reading wall-clock time."""

    def test_freshness_stale_flag_follows_injected_today_not_wall_clock(self, session: Session) -> None:
        # A single gmail_n8n txn dated 2026-01-01; gmail cadence is 3 days.
        _tx(
            session,
            amount="1.00",
            direction=Direction.INCOME.value,
            entity=Entity.SPARKRY.value,
            date="2026-01-01",
            source=Source.GMAIL_N8N.value,
        )

        # today = 2026-01-02 (1 day old) → within 3-day cadence → not stale.
        fresh_data = wbr.compute_wbr(session, date(2026, 1, 2))
        gmail_row = next(r for r in fresh_data["freshness"] if r["label"] == "gmail")
        assert gmail_row["stale"] is False

        # today = 2030-01-01 (far future, nowhere near real wall-clock time)
        # → thousands of days old → stale. If the implementation regressed to
        # date.today(), this assertion would be indifferent to the injected
        # ``today`` and could pass or fail depending on when the suite runs.
        stale_data = wbr.compute_wbr(session, date(2030, 1, 1))
        gmail_row_stale = next(r for r in stale_data["freshness"] if r["label"] == "gmail")
        assert gmail_row_stale["stale"] is True


class TestReimbursableNetting:
    """P1-r1emb regression: a linked reimbursable expense/income pair (the
    Cardinal-Health-style case) must net to zero and leave WBR's rendered
    entity numbers unchanged — guarding the explicitly-flagged pl_engine
    merge risk (WS5 was built against an older pl_engine copy)."""

    def test_linked_reimbursable_pair_does_not_move_entity_net_this(self, session: Session) -> None:
        _full_fixture(session)
        baseline = wbr.compute_wbr(session, TODAY)
        baseline_sparkry = next(r for r in baseline["entities"] if r["entity"] == Entity.SPARKRY.value)
        baseline_household_net = baseline["household"]["net_this"]

        # A reimbursement pair landing inside this week's window
        # [2026-07-06, 2026-07-13): a reimbursable expense linked to its
        # matching income row. Both legs must be invisible to P&L.
        income_tx = _tx(
            session,
            amount="500.00",
            direction=Direction.INCOME.value,
            entity=Entity.SPARKRY.value,
            date="2026-07-08",
            description="Cardinal Health reimbursement receipt",
        )
        _tx(
            session,
            amount="-500.00",
            direction=Direction.REIMBURSABLE.value,
            entity=Entity.SPARKRY.value,
            date="2026-07-07",
            description="Cardinal Health reimbursable expense",
            reimbursement_link=income_tx.id,
        )

        data = wbr.compute_wbr(session, TODAY)
        sparkry = next(r for r in data["entities"] if r["entity"] == Entity.SPARKRY.value)

        assert sparkry["revenue_this"] == baseline_sparkry["revenue_this"]
        assert sparkry["expenses_this"] == baseline_sparkry["expenses_this"]
        assert sparkry["net_this"] == baseline_sparkry["net_this"]
        assert data["household"]["net_this"] == baseline_household_net


class TestNoReDerivation:
    """P1-t1eout: these tests assert WBR does not RE-DERIVE its own P&L
    arithmetic — every entity/household row is produced by literally calling
    ``compute_entity_pl`` rather than reimplementing the revenue/expense/net
    math inline. That is a real and valuable guarantee (it is what makes
    ``TestHandComputedTieOut`` below sufficient — WBR has no second code path
    to independently get wrong), but it is NOT a tie-out to pl_engine's
    correctness: both sides of every assertion here call the exact same
    ``compute_entity_pl`` function, so they move together under ANY change
    to its math, including a reimbursable-netting regression. A genuine
    pl_engine regression will NOT be caught by this class — see
    ``TestHandComputedTieOut`` for hand-computed, pl_engine-independent
    expected values that would catch such a regression."""

    def test_entity_rows_call_the_same_compute_entity_pl(self, session: Session) -> None:
        from src.reports.pl_engine import compute_entity_pl

        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        week_start, week_end = data["week_start"], data["week_end"]
        for row in data["entities"]:
            pl = compute_entity_pl(session, week_start, week_end, entity=row["entity"])
            assert row["revenue_this"] == pl.revenue
            assert row["expenses_this"] == pl.expenses
            assert row["net_this"] == pl.net

    def test_household_calls_the_same_compute_entity_pl_with_entity_none(self, session: Session) -> None:
        from src.reports.pl_engine import compute_entity_pl

        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        pl = compute_entity_pl(session, data["week_start"], data["week_end"], entity=None)
        assert data["household"]["net_this"] == pl.net


class TestHandComputedTieOut:
    """REQ-WBR-002 real tie-out: expected revenue/expenses/net below are
    computed BY HAND from ``_full_fixture``'s seeded rows — NOT by calling
    ``compute_entity_pl`` — so a genuine regression in pl_engine's math
    (e.g. a reimbursable-netting bug, or NEEDS_REVIEW rows being wrongly
    excluded) trips this test even though ``TestNoReDerivation`` above would
    stay green (both sides of that class's assertions move together).

    Hand computation for Sparkry, this week [2026-07-06, 2026-07-13):
      revenue = 8250.00 (main week revenue tx)
              +    1.00 (freshness gmail_n8n sentinel, 2026-07-12, income)
              = 8251.00
      expenses =  550.00 (main week expense tx)
               +  900.00 (AWS annual renewal, 2026-07-09)
               +  140.00 (14 * $10.00 NEEDS_REVIEW rows, 2026-07-07 —
                          NEEDS_REVIEW is not an excluded status, so these
                          DO count as expenses per pl_engine's semantics)
               = 1590.00
      net = 8251.00 - 1590.00 = 6661.00
    (These figures also match the pinned golden fixture's rendered
    "$8,251 / ($1,590) / $6,661" row — cross-checked two independent ways.)
    """

    def test_sparkry_this_week_matches_hand_computed_figures(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        sparkry = next(r for r in data["entities"] if r["entity"] == Entity.SPARKRY.value)
        assert sparkry["revenue_this"] == Decimal("8251.00")
        assert sparkry["expenses_this"] == Decimal("1590.00")
        assert sparkry["net_this"] == Decimal("6661.00")


class TestFormatting:
    def test_sparkline_flat_series(self) -> None:
        assert wbr.sparkline([Decimal("5")] * 6) == wbr._SPARK_CHARS[4] * 6

    def test_sparkline_increasing_series(self) -> None:
        s = wbr.sparkline([Decimal(i) for i in range(6)])
        assert s[0] == "▁"
        assert s[-1] == "█"

    def test_sparkline_empty(self) -> None:
        assert wbr.sparkline([]) == ""

    def test_pct_change_zero_baseline_nonzero_now(self) -> None:
        assert wbr._pct_change(Decimal("10"), Decimal("0")) is None

    def test_pct_change_zero_baseline_zero_now(self) -> None:
        assert wbr._pct_change(Decimal("0"), Decimal("0")) == Decimal("0")

    def test_fmt0_paren_for_negative(self) -> None:
        assert wbr._fmt0_signed(Decimal("-425")) == "($425)"

    def test_fmt0_delta_positive_has_explicit_sign(self) -> None:
        assert wbr._fmt0_delta(Decimal("761")) == "+$761"

    def test_iso_week_label(self) -> None:
        assert wbr._iso_week_label(date(2026, 7, 6)) == "2026-W28"


class TestSubjectLine:
    def test_subject_includes_week_net_and_warning_count(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        subject = wbr.build_subject(data)
        assert subject.startswith("[WBR] 2026-W28")
        assert "HH net" in subject
        assert subject.endswith("⚠️")


class TestGoldenOutput:
    def test_render_matches_golden_fixture(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        rendered = wbr.render_report(data)
        if not GOLDEN_PATH.exists():
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN_PATH.write_text(rendered)
            pytest.fail(f"Golden fixture didn't exist; wrote it to {GOLDEN_PATH}. Re-run to verify.")
        assert rendered == GOLDEN_PATH.read_text()

    def test_render_is_deterministic(self, session: Session) -> None:
        _full_fixture(session)
        data1 = wbr.compute_wbr(session, TODAY)
        data2 = wbr.compute_wbr(session, TODAY)
        assert wbr.render_report(data1) == wbr.render_report(data2)


class TestDispatchLedger:
    def test_apply_writes_resend_email_channel_with_null_payload(self, session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESEND_API_KEY", "test-key")

        def _fake_send(params: object) -> dict[str, str]:
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        body = wbr.render_report(data)
        subject = wbr.build_subject(data)
        result = report_email.dispatch_report(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date=data["week_start"],
            alert_type="wbr_weekly",
            entity="all",
            subject=subject,
            body=body,
            apply=True,
        )
        assert result.status == "sent"
        row = session.query(AlertDispatch).filter_by(alert_key="wbr:2026-W28", occurrence_date=data["week_start"]).one()
        assert row.delivery_channel == "resend_email"
        assert row.payload_json is None

    def test_catchup_rerun_same_period_anchor_is_deduped(self, session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Persistent=true catch-up firing after a missed Monday must not
        double-send when occurrence_date is pinned to the period anchor."""
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        send_calls: list[object] = []

        def _fake_send(params: object) -> dict[str, str]:
            send_calls.append(params)
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        body = wbr.render_report(data)
        subject = wbr.build_subject(data)

        def _dispatch() -> report_email.SendResult:
            return report_email.dispatch_report(
                session,
                alert_key="wbr:2026-W28",
                occurrence_date=data["week_start"],
                alert_type="wbr_weekly",
                entity="all",
                subject=subject,
                body=body,
                apply=True,
            )

        first = _dispatch()
        second = _dispatch()
        assert first.status == "sent"
        assert second.status == "skipped"
        assert len(send_calls) == 1

    def test_dry_run_never_writes_ledger(self, session: Session) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY)
        body = wbr.render_report(data)
        result = report_email.dispatch_report(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date=data["week_start"],
            alert_type="wbr_weekly",
            entity="all",
            subject="test",
            body=body,
            apply=False,
        )
        assert result.status == "dry_run"
        assert session.query(AlertDispatch).filter_by(alert_key="wbr:2026-W28").count() == 0


class TestConfigDirOverride:
    """REQ-WBR-001..003 / P2-wbrcfg2 regression: ``compute_wbr`` previously
    had no ``config_dir`` override (unlike ``compute_txf``/
    ``compute_sellability``). This class covers the newly added parameter:
    a config-driven threshold change flips a row's warning marker, and an
    absent ``reporting.yaml`` surfaces the ``used_config_defaults`` marker
    in the rendered footer."""

    @pytest.fixture()
    def config_dir_low_net_threshold(self, tmp_path: Path) -> Path:
        d = tmp_path / "config"
        d.mkdir()
        (d / "reporting.yaml").write_text(
            "thresholds:\n"
            "  net_drop_pct: 5\n"
            "  net_min_abs: 100\n"
        )
        return d

    def test_custom_low_net_drop_threshold_flips_row_from_ok_to_warn(
        self, session: Session, config_dir_low_net_threshold: Path
    ) -> None:
        _full_fixture(session)
        baseline = wbr.compute_wbr(session, TODAY)
        baseline_sparkry = next(r for r in baseline["entities"] if r["entity"] == Entity.SPARKRY.value)
        assert baseline_sparkry["net_warn"] is None  # ✅ under the default 30% threshold

        data = wbr.compute_wbr(session, TODAY, config_dir=config_dir_low_net_threshold)
        sparkry = next(r for r in data["entities"] if r["entity"] == Entity.SPARKRY.value)
        assert sparkry["net_warn"] is not None  # ⚠️ under the custom 5% threshold

        rendered = wbr.render_report(data)
        assert wbr._row_mark(sparkry["net_warn"]) in rendered

    def test_missing_reporting_yaml_in_config_dir_surfaces_defaults_marker(
        self, session: Session, tmp_path: Path
    ) -> None:
        empty_config_dir = tmp_path / "empty_config"
        empty_config_dir.mkdir()
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY, config_dir=empty_config_dir)
        assert data["used_config_defaults"] is True
        rendered = wbr.render_report(data)
        assert "thresholds: defaults" in rendered

    def test_present_reporting_yaml_does_not_mark_used_config_defaults(
        self, session: Session, config_dir_low_net_threshold: Path
    ) -> None:
        _full_fixture(session)
        data = wbr.compute_wbr(session, TODAY, config_dir=config_dir_low_net_threshold)
        assert data["used_config_defaults"] is False
        rendered = wbr.render_report(data)
        assert "thresholds: config v1" in rendered


class TestCLI:
    def test_dry_run_smoke_exits_zero_and_prints(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        s = SessionLocal()
        _full_fixture(s)
        s.close()

        rc = wbr.main(["--date", "2026-07-13", "--db", str(db_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "WBR — week" in captured.out

        # No WBR ledger row written in dry-run (fixture seeds unrelated balance-alert rows).
        s2 = SessionLocal()
        assert s2.query(AlertDispatch).filter(AlertDispatch.alert_key.like("wbr:%")).count() == 0
        s2.close()
