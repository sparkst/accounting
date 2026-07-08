"""Tests for the deterministic anomaly scan (REQ-MCA-001, spec §1.3)."""

from __future__ import annotations

import itertools
from collections.abc import Generator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401  (register every ORM table on Base)
from src.close.anomalies import normalize_vendor, scan_anomalies
from src.models.base import Base
from src.models.enums import Direction, Entity, TaxCategory, TransactionStatus
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)

_counter = itertools.count()


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.rollback()
    s.query(Transaction).delete()
    s.query(VendorRule).delete()
    s.commit()
    s.close()


def _tx(
    session: Session,
    *,
    description: str,
    date_: str,
    amount: str,
    entity: str = Entity.SPARKRY.value,
) -> Transaction:
    n = next(_counter)
    tx = Transaction(
        source="plaid",
        source_hash=f"h-{n}",
        date=date_,
        description=description,
        amount=Decimal(amount),
        entity=entity,
        tax_category=TaxCategory.OFFICE_EXPENSE.value,
        direction=Direction.EXPENSE.value,
        status=TransactionStatus.CONFIRMED.value,
        raw_data={},
    )
    session.add(tx)
    session.flush()
    return tx


# ── normalize_vendor ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AMAZON   ", "amazon"),
        ("Amazon #1234", "amazon"),
        ("UBER   TRIP 4829", "uber trip"),
        ("STRIPE TRANSFER 000123456", "stripe transfer"),
        ("Netflix.com", "netflix.com"),
        ("SQ *Coffee Shop", "sq *coffee shop"),
        ("12345", "12345"),  # all-ref → fallback keeps collapsed form
        ("", ""),
        ("Walmart Store #", "walmart store"),
    ],
)
def test_normalize_vendor(raw: str, expected: str) -> None:
    """REQ-MCA-001: normalize_vendor lower-cases, collapses ws, strips ref suffixes."""
    assert normalize_vendor(raw) == expected


# ── new vendors ───────────────────────────────────────────────────────────


def test_new_vendor_first_seen_in_month(session: Session) -> None:
    """REQ-MCA-001: a >=$25 vendor first seen in the close month is flagged new."""
    _tx(session, description="Newco LLC", date_="2026-06-10", amount="-80.00")
    report = scan_anomalies(session, "2026-06")
    keys = {v.vendor_key for v in report.new_vendors}
    assert "newco llc" in keys


def test_prior_history_disqualifies_new_vendor(session: Session) -> None:
    """REQ-MCA-001: a vendor with an earlier row is not "new"."""
    _tx(session, description="Oldco", date_="2026-03-01", amount="-90.00")
    _tx(session, description="Oldco", date_="2026-06-10", amount="-90.00")
    report = scan_anomalies(session, "2026-06")
    assert "oldco" not in {v.vendor_key for v in report.new_vendors}


def test_new_vendor_below_threshold_not_flagged(session: Session) -> None:
    """REQ-MCA-001: a first-seen vendor under $25 is below the new-vendor bar."""
    _tx(session, description="Tinyco", date_="2026-06-10", amount="-10.00")
    report = scan_anomalies(session, "2026-06")
    assert "tinyco" not in {v.vendor_key for v in report.new_vendors}


def test_vendor_rule_match_disqualifies_new_vendor(session: Session) -> None:
    """REQ-MCA-001: a description matched by a VendorRule is not a new vendor."""
    session.add(
        VendorRule(
            vendor_pattern="knownco",
            is_regex=False,
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.OFFICE_EXPENSE.value,
            direction=Direction.EXPENSE.value,
            confidence=0.95,
        )
    )
    session.flush()
    _tx(session, description="KnownCo Inc", date_="2026-06-10", amount="-99.00")
    report = scan_anomalies(session, "2026-06")
    assert "knownco inc" not in {v.vendor_key for v in report.new_vendors}


# ── amount outliers ───────────────────────────────────────────────────────


def _seed_prior(session: Session, vendor: str, values: list[str]) -> None:
    base = date(2026, 1, 15)
    for i, v in enumerate(values):
        _tx(
            session,
            description=vendor,
            date_=(base + timedelta(days=30 * i)).isoformat(),
            amount=v,
        )


def test_outlier_at_exactly_3sigma_and_50_is_flagged(session: Session) -> None:
    """REQ-MCA-001: z==3 AND |amt-mean|==120 (>=$50) flags an outlier (mu=20,sigma=40)."""
    _seed_prior(session, "OutlierCo", ["0", "0", "0", "0", "-100"])  # mu=20 sigma=40
    _tx(session, description="OutlierCo", date_="2026-06-05", amount="-140.00")
    report = scan_anomalies(session, "2026-06")
    flagged = {o.vendor_key for o in report.outliers}
    assert "outlierco" in flagged


def test_just_below_3sigma_not_flagged(session: Session) -> None:
    """REQ-MCA-001: z just under 3 (diff 119 / sigma 40 = 2.975) is not an outlier."""
    _seed_prior(session, "EdgeCo", ["0", "0", "0", "0", "-100"])  # mu=20 sigma=40
    _tx(session, description="EdgeCo", date_="2026-06-05", amount="-139.00")
    report = scan_anomalies(session, "2026-06")
    assert "edgeco" not in {o.vendor_key for o in report.outliers}


def test_sigma_floor_dollar_gate(session: Session) -> None:
    """REQ-MCA-001: with sigma floored to $1, the $50 abs-gate still governs."""
    _seed_prior(session, "FlatCo", ["-100", "-100", "-100", "-100", "-100"])  # sigma 0→1
    # diff 49 → below $50 gate, not flagged despite huge z.
    _tx(session, description="FlatCo", date_="2026-06-02", amount="-149.00")
    # diff 50 → meets both gates.
    _tx(session, description="FlatCo", date_="2026-06-03", amount="-150.00")
    report = scan_anomalies(session, "2026-06")
    amts = {str(o.amount) for o in report.outliers if o.vendor_key == "flatco"}
    assert "-150.00" in amts
    assert "-149.00" not in amts


# ── missing recurring ─────────────────────────────────────────────────────


def _seed_cadence(session: Session, vendor: str, intervals: list[int]) -> None:
    d = date(2026, 1, 5)
    _tx(session, description=vendor, date_=d.isoformat(), amount="-30.00")
    for gap in intervals:
        d = d + timedelta(days=gap)
        _tx(session, description=vendor, date_=d.isoformat(), amount="-30.00")


def test_missing_recurring_monthly_gap(session: Session) -> None:
    """REQ-MCA-001: a monthly vendor (30-day cadence) absent in the month is flagged."""
    # 3 charges Apr/May at ~30 days, none in June.
    _tx(session, description="Sub", date_="2026-04-06", amount="-30.00")
    _tx(session, description="Sub", date_="2026-05-06", amount="-30.00")
    _tx(session, description="Sub", date_="2026-05-06", amount="-30.00")  # dup date ok
    _tx(session, description="Sub", date_="2026-03-07", amount="-30.00")
    report = scan_anomalies(session, "2026-06")
    assert "sub" in {m.vendor_key for m in report.missing_recurring}


@pytest.mark.parametrize(("gap", "flagged"), [(25, True), (35, True), (24, False), (40, False)])
def test_cadence_edges(session: Session, gap: int, flagged: bool) -> None:
    """REQ-MCA-001: median interval must fall in [25, 35] days to count as monthly."""
    _seed_cadence(session, "Cadence", [gap, gap])  # 3 charges, median interval == gap
    report = scan_anomalies(session, "2026-06")
    present = "cadence" in {m.vendor_key for m in report.missing_recurring}
    assert present is flagged


def test_config_ignore_suppresses(session: Session, tmp_path: Path) -> None:
    """REQ-MCA-001: config ignore: suppresses a history-flagged missing vendor."""
    _tx(session, description="Sub", date_="2026-04-06", amount="-30.00")
    _tx(session, description="Sub", date_="2026-05-06", amount="-30.00")
    _tx(session, description="Sub", date_="2026-03-07", amount="-30.00")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("ignore:\n  - sub\nrequire: []\n")
    report = scan_anomalies(session, "2026-06", config_path=cfg)
    assert "sub" not in {m.vendor_key for m in report.missing_recurring}


def test_config_require_force_tracks(session: Session, tmp_path: Path) -> None:
    """REQ-MCA-001: config require: force-tracks a vendor even with thin history."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "ignore: []\nrequire:\n  - vendor: rareco\n    expected_day: 15\n    amount_hint: '49.00'\n"
    )
    report = scan_anomalies(session, "2026-06", config_path=cfg)
    missing = {m.vendor_key: m for m in report.missing_recurring}
    assert "rareco" in missing
    assert missing["rareco"].source == "config"
    assert missing["rareco"].typical_amount == Decimal("49.00")
