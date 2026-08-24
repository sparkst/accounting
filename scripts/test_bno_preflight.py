"""Tests for scripts/bno_preflight.py — deterministic B&O pre-filing checklist.

REQ-IDs: REQ-BNO-CHK-001..006. Each check is a pure function over transaction
dicts so it is testable without a database; one end-to-end test builds a real
SQLite file and asserts the CLI opens it read-only and exits non-zero on any
failing check.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from scripts.bno_preflight import (
    Period,
    check_confirmed_only,
    check_locality_mapping,
    check_rate_tier,
    check_refund_sweep,
    check_sign_vs_direction,
    check_unlinked_reimbursables,
    main,
    parse_period,
    run_checks,
)


def tx(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "t-1",
        "date": "2026-07-15",
        "description": "Acme Corp",
        "amount": "100.00",
        "entity": "sparkry",
        "direction": "income",
        "tax_category": "CONSULTING_INCOME",
        "status": "confirmed",
        "confidence": 1.0,
        "source": "stripe",
        "reimbursement_link": None,
        "raw_data": {},
    }
    base.update(kw)
    return base


# ── period parsing ─────────────────────────────────────────────────────────


def test_parse_period_month() -> None:
    p = parse_period("2026-07")
    assert (p.year, p.months) == (2026, [7])
    assert p.start == "2026-07-01" and p.end == "2026-07-31"


def test_parse_period_quarter() -> None:
    p = parse_period("2026-Q2")
    assert (p.year, p.months) == (2026, [4, 5, 6])
    assert p.start == "2026-04-01" and p.end == "2026-06-30"


def test_parse_period_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_period("2026-13")
    with pytest.raises(ValueError):
        parse_period("Q2-2026")


# ── REQ-BNO-CHK-001 sign-vs-direction ──────────────────────────────────────


def test_chk001_flags_positive_expense_and_negative_income() -> None:
    p = parse_period("2026-07")
    rows = [
        tx(id="ok", direction="expense", amount="-50.00"),
        tx(id="bad-exp", direction="expense", amount="50.00"),
        tx(id="bad-inc", direction="income", amount="-75.00"),
        tx(id="out-of-period", direction="expense", amount="9.00", date="2026-06-01"),
    ]
    res = check_sign_vs_direction(rows, p)
    assert res.req_id == "REQ-BNO-CHK-001"
    assert not res.passed
    joined = "\n".join(res.details)
    assert "bad-exp" in joined and "bad-inc" in joined
    assert "out-of-period" not in joined


def test_chk001_passes_clean() -> None:
    p = parse_period("2026-07")
    rows = [tx(direction="income", amount="100.00")]
    assert check_sign_vs_direction(rows, p).passed


# ── REQ-BNO-CHK-002 unlinked reimbursables ─────────────────────────────────


def test_chk002_flags_stale_reimbursable_with_matching_deposit() -> None:
    p = parse_period("2026-07")
    rows = [
        tx(
            id="reimb",
            direction="reimbursable",
            amount="-500.00",
            date="2026-04-01",
            description="Cardinal Health travel expense",
            reimbursement_link=None,
        ),
        tx(
            id="deposit",
            direction="income",
            amount="500.00",
            date="2026-07-10",
            description="ACH CARDINAL HEALTH INC",
        ),
    ]
    res = check_unlinked_reimbursables(rows, p)
    assert res.req_id == "REQ-BNO-CHK-002"
    assert not res.passed
    joined = "\n".join(res.details)
    assert "reimb" in joined and "deposit" in joined


def test_chk002_passes_when_linked_or_recent_or_unmatched() -> None:
    p = parse_period("2026-07")
    rows = [
        # linked — fine
        tx(
            id="linked",
            direction="reimbursable",
            amount="-1.00",
            date="2026-01-01",
            reimbursement_link="deposit",
        ),
        # unlinked but recent (inside 30 days of period end)
        tx(
            id="recent",
            direction="reimbursable",
            amount="-2.00",
            date="2026-07-20",
            description="Cardinal Health hotel",
        ),
        # unlinked + stale but no matching in-period deposit
        tx(
            id="stale-nomatch",
            direction="reimbursable",
            amount="-3.00",
            date="2026-01-01",
            description="Zebra Consulting flight",
        ),
        tx(id="deposit", amount="500.00", description="ACH CARDINAL HEALTH INC"),
    ]
    assert check_unlinked_reimbursables(rows, p).passed


# ── REQ-BNO-CHK-003 confirmed-only gate ────────────────────────────────────


def test_chk003_flags_unconfirmed_income_rows() -> None:
    p = parse_period("2026-07")
    rows = [
        tx(id="ok-confirmed", status="confirmed"),
        tx(id="bad-auto", status="auto_classified", confidence=0.91),
        tx(id="bad-review", status="needs_review", confidence=0.4),
        tx(id="expense-ignored", status="needs_review", tax_category="SUPPLIES"),
    ]
    res = check_confirmed_only(rows, p)
    assert res.req_id == "REQ-BNO-CHK-003"
    assert not res.passed
    joined = "\n".join(res.details)
    assert "bad-auto" in joined and "bad-review" in joined
    assert "expense-ignored" not in joined


def test_chk003_passes_when_all_income_confirmed() -> None:
    p = parse_period("2026-07")
    assert check_confirmed_only([tx(status="confirmed")], p).passed


# ── REQ-BNO-CHK-004 refund sweep (informational) ───────────────────────────


def test_chk004_totals_refund_like_rows_and_always_passes() -> None:
    p = parse_period("2026-07")
    rows = [
        tx(
            id="stripe-refund",
            source="stripe",
            direction="expense",
            tax_category="OTHER_EXPENSE",
            amount="-40.00",
            description="Refund: ch_123",
        ),
        tx(
            id="retail-return",
            source="shopify",
            direction="expense",
            tax_category="OTHER_EXPENSE",
            amount="-25.50",
            description="Chargeback order #1043",
        ),
        tx(id="normal-expense", direction="expense", tax_category="SUPPLIES", amount="-99.00"),
    ]
    res = check_refund_sweep(rows, p)
    assert res.req_id == "REQ-BNO-CHK-004"
    assert res.passed  # informational
    joined = "\n".join(res.details)
    assert "65.50" in joined  # 40.00 + 25.50


def test_chk004_prints_zero_total_when_no_refunds() -> None:
    p = parse_period("2026-07")
    res = check_refund_sweep([tx()], p)
    assert res.passed
    assert any("0.00" in line for line in res.details)


# ── REQ-BNO-CHK-005 rate-tier assert ───────────────────────────────────────


def test_chk005_fails_when_prior_year_serviceother_crosses_1m() -> None:
    p = parse_period("2026-07")
    rows = [
        tx(id=f"py-{i}", date="2025-06-01", amount="600000.00", tax_category="CONSULTING_INCOME")
        for i in range(2)
    ]  # $1.2M in 2025
    res = check_rate_tier(rows, p)
    assert res.req_id == "REQ-BNO-CHK-005"
    assert not res.passed


def test_chk005_passes_under_1m_and_ignores_retail() -> None:
    p = parse_period("2026-07")
    rows = [
        tx(id="py-svc", date="2025-06-01", amount="900000.00"),
        # Retailing is not in the ServiceOther tier measure
        tx(id="py-retail", date="2025-06-01", amount="500000.00", tax_category="SALES_INCOME"),
        # current-year income irrelevant to the prior-year tier
        tx(id="cy", date="2026-02-01", amount="900000.00"),
    ]
    assert check_rate_tier(rows, p).passed


# ── REQ-BNO-CHK-006 locality mapping ───────────────────────────────────────


def _shopify_wa_order(order_id: str, city_tax_title: str) -> dict[str, Any]:
    return tx(
        id=order_id,
        entity="blackline",
        tax_category="SALES_INCOME",
        source="shopify",
        amount="110.00",
        raw_data={
            "total_price": "110.00",
            "total_tax": "10.00",
            "tax_lines": [
                {"title": "Washington State Tax"},
                {"title": city_tax_title},
            ],
            "shipping_address": {"province_code": "WA"},
        },
    )


def test_chk006_flags_unmapped_wa_locality() -> None:
    p = parse_period("2026-Q3")
    rows = [
        _shopify_wa_order("ok-order", "Sammamish City Tax"),
        _shopify_wa_order("bad-order", "Spokane City Tax"),
    ]
    res = check_locality_mapping(rows, p, entity="blackline")
    assert res.req_id == "REQ-BNO-CHK-006"
    assert not res.passed
    assert any("spokane" in line.lower() for line in res.details)


def test_chk006_passes_for_mapped_localities_and_service_entity() -> None:
    p = parse_period("2026-Q3")
    rows = [_shopify_wa_order("ok-order", "Sammamish City Tax")]
    assert check_locality_mapping(rows, p, entity="blackline").passed
    # sparkry has no retail rows — trivially PASS
    assert check_locality_mapping([tx()], parse_period("2026-07"), entity="sparkry").passed


# ── run_checks + CLI end-to-end ────────────────────────────────────────────


def test_run_checks_returns_all_six_in_order() -> None:
    res = run_checks([tx()], parse_period("2026-07"), entity="sparkry")
    assert [r.req_id for r in res] == [f"REQ-BNO-CHK-00{i}" for i in range(1, 7)]


def _make_db(path: Path, rows: list[dict[str, Any]]) -> None:
    import json

    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE transactions (
            id TEXT PRIMARY KEY, source TEXT, source_id TEXT, source_hash TEXT,
            date TEXT, description TEXT, amount NUMERIC, entity TEXT,
            direction TEXT, tax_category TEXT, status TEXT, confidence REAL,
            reimbursement_link TEXT, raw_data TEXT
        )"""
    )
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO transactions (id, source, source_hash, date, description,"
            " amount, entity, direction, tax_category, status, confidence,"
            " reimbursement_link, raw_data)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["id"],
                r["source"],
                f"hash-{i}",
                r["date"],
                r["description"],
                str(r["amount"]),
                r["entity"],
                r["direction"],
                r["tax_category"],
                r["status"],
                r["confidence"],
                r["reimbursement_link"],
                json.dumps(r["raw_data"]),
            ),
        )
    conn.commit()
    conn.close()


def test_cli_exits_zero_on_clean_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "clean.db"
    _make_db(db, [tx()])
    rc = main(["--entity", "sparkry", "--period", "2026-07", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("PASS") == 6


def test_cli_exits_nonzero_on_failing_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "dirty.db"
    _make_db(db, [tx(id="bad", direction="expense", amount="50.00")])
    rc = main(["--entity", "sparkry", "--period", "2026-07", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "FAIL" in out and "REQ-BNO-CHK-001" in out


def test_cli_excludes_rejected_rows(tmp_path: Path) -> None:
    db = tmp_path / "rejected.db"
    _make_db(db, [tx(id="bad", direction="expense", amount="50.00", status="rejected")])
    assert main(["--entity", "sparkry", "--period", "2026-07", "--db", str(db)]) == 0


def test_cli_opens_db_read_only(tmp_path: Path) -> None:
    """The DB file must be byte-identical after a run (strictly read-only)."""
    db = tmp_path / "ro.db"
    _make_db(db, [tx()])
    before = db.read_bytes()
    main(["--entity", "sparkry", "--period", "2026-07", "--db", str(db)])
    assert db.read_bytes() == before


def test_cli_missing_db_errors(tmp_path: Path) -> None:
    rc = main(["--entity", "sparkry", "--period", "2026-07", "--db", str(tmp_path / "nope.db")])
    assert rc != 0


def test_period_type_is_exported() -> None:
    assert isinstance(parse_period("2026-Q1"), Period)
