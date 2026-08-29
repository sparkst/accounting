"""Tests for scripts/bno_preflight.py — deterministic B&O pre-filing checklist.

REQ-IDs: REQ-BNO-CHK-001..007. Each check is a pure function over transaction
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
    build_account_entity_map,
    check_confirmed_only,
    check_entity_account_commingling,
    check_locality_mapping,
    check_rate_tier,
    check_refund_sweep,
    check_sign_vs_direction,
    check_unlinked_reimbursables,
    load_all_transactions,
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


def test_chk002_reports_but_never_blocks_stale_reimbursable_with_matching_deposit() -> None:
    """Regression guard for #64: an unreimbursed trip is expected month-end
    state, not a filing blocker. The check stays informational — it must
    surface the pairing (so a human can still glance at it) but never fail
    the checklist, or every filing that follows a not-yet-reimbursed trip
    gets held hostage.
    """
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
    assert res.passed  # informational only — must never block a B&O filing
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


def test_chk004_matches_bare_return_and_returns() -> None:
    p = parse_period("2026-07")
    rows = [
        tx(id="ret", direction="expense", tax_category="OTHER_EXPENSE",
           amount="-10.00", description="Return for order #1043"),
        tx(id="rets", direction="expense", tax_category="OTHER_EXPENSE",
           amount="-5.00", description="Customer returns batch"),
    ]
    res = check_refund_sweep(rows, p)
    joined = "\n".join(res.details)
    assert "ret" in joined and "rets" in joined
    assert "15.00" in joined  # 10.00 + 5.00


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


# ── REQ-BNO-CHK-007 entity-vs-account commingling ──────────────────────────
#
# Ownership of a receiving account is looked up from an account-label → owning-
# entity map (built from the `account` table). A receipt whose owning `entity`
# disagrees with the account it lands in is commingling; an unmapped label is
# surfaced ("cannot verify"), never silently passed. The concrete bug (#76/#61):
# Sparkry LLC Substack revenue paying out into Travis's personal Chase.

# Chase ****3894 = personal; Chase ****0001 = Sparkry business (test fixtures).
ACCT_MAP = {"Chase ****3894": "personal", "Chase ****0001": "sparkry"}


def test_chk007_flags_business_income_into_personal_account() -> None:
    """Substack payout: entity=sparkry landing in the personal Chase → FLAG."""
    p = parse_period("2026-07")
    rows = [
        tx(
            id="substack-payout",
            entity="sparkry",
            direction="transfer",
            amount="343.00",
            payment_method="Chase ****3894",
            description="Substack payout",
        )
    ]
    res = check_entity_account_commingling(rows, p, ACCT_MAP)
    assert res.req_id == "REQ-BNO-CHK-007"
    assert not res.passed
    joined = "\n".join(res.details)
    assert "substack-payout" in joined
    assert "sparkry" in joined and "personal" in joined


def test_chk007_passes_business_income_into_matching_business_account() -> None:
    """The same revenue on the Sparkry business Chase → PASS (no flag)."""
    p = parse_period("2026-07")
    rows = [
        tx(
            id="ok-payout",
            entity="sparkry",
            direction="transfer",
            amount="343.00",
            payment_method="Chase ****0001",
            description="Substack payout",
        )
    ]
    res = check_entity_account_commingling(rows, p, ACCT_MAP)
    assert res.passed
    assert res.details == []


def test_chk007_flags_personal_income_into_business_account() -> None:
    """Inverse direction: entity=personal landing in a business account → FLAG."""
    p = parse_period("2026-07")
    rows = [
        tx(
            id="personal-in-biz",
            entity="personal",
            direction="income",
            amount="200.00",
            payment_method="Chase ****0001",
            description="Personal side income",
        )
    ]
    res = check_entity_account_commingling(rows, p, ACCT_MAP)
    assert not res.passed
    joined = "\n".join(res.details)
    assert "personal-in-biz" in joined


def test_chk007_reports_unmapped_payment_method_never_silently_passes() -> None:
    """A payment_method not in the map is surfaced, not silently passed."""
    p = parse_period("2026-07")
    rows = [
        tx(
            id="mystery",
            entity="sparkry",
            direction="income",
            amount="100.00",
            payment_method="Wells Fargo ****9999",
        )
    ]
    res = check_entity_account_commingling(rows, p, ACCT_MAP)
    assert not res.passed
    joined = "\n".join(res.details).lower()
    assert "mystery" in "\n".join(res.details)
    assert "unmapped" in joined and "cannot verify" in joined


def test_chk007_skips_rows_without_a_payment_method_label() -> None:
    """Stripe charge rows carry no account label (the payout row does) → skip."""
    p = parse_period("2026-07")
    rows = [tx(id="charge", entity="sparkry", amount="500.00", payment_method=None)]
    assert check_entity_account_commingling(rows, p, ACCT_MAP).passed


def test_chk007_ignores_out_of_period_and_expense_outflows() -> None:
    """Only in-period receipts (amount > 0) are receiving-account events."""
    p = parse_period("2026-07")
    rows = [
        # expense outflow from the personal Chase — not a receipt "into" it
        tx(
            id="expense",
            entity="sparkry",
            direction="expense",
            amount="-50.00",
            payment_method="Chase ****3894",
        ),
        # commingled receipt but out of the filing period
        tx(
            id="oop",
            entity="sparkry",
            direction="income",
            amount="50.00",
            date="2026-06-01",
            payment_method="Chase ****3894",
        ),
    ]
    assert check_entity_account_commingling(rows, p, ACCT_MAP).passed


# ── run_checks + CLI end-to-end ────────────────────────────────────────────


def test_run_checks_returns_all_seven_in_order() -> None:
    res = run_checks([tx()], parse_period("2026-07"), entity="sparkry")
    assert [r.req_id for r in res] == [f"REQ-BNO-CHK-00{i}" for i in range(1, 8)]


def _make_db(
    path: Path,
    rows: list[dict[str, Any]],
    accounts: list[tuple[str, str]] | None = None,
) -> None:
    import json

    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE transactions (
            id TEXT PRIMARY KEY, source TEXT, source_id TEXT, source_hash TEXT,
            date TEXT, description TEXT, amount NUMERIC, entity TEXT,
            direction TEXT, tax_category TEXT, status TEXT, confidence REAL,
            reimbursement_link TEXT, payment_method TEXT, raw_data TEXT
        )"""
    )
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO transactions (id, source, source_hash, date, description,"
            " amount, entity, direction, tax_category, status, confidence,"
            " reimbursement_link, payment_method, raw_data)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                r.get("payment_method"),
                json.dumps(r["raw_data"]),
            ),
        )
    if accounts is not None:
        conn.execute(
            "CREATE TABLE account (id TEXT PRIMARY KEY, payment_method TEXT, entity TEXT)"
        )
        for j, (pm, ent) in enumerate(accounts):
            conn.execute(
                "INSERT INTO account (id, payment_method, entity) VALUES (?,?,?)",
                (f"acct-{j}", pm, ent),
            )
    conn.commit()
    conn.close()


def test_cli_exits_zero_on_clean_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "clean.db"
    _make_db(db, [tx()])
    rc = main(["--entity", "sparkry", "--period", "2026-07", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("PASS") == 7


def test_cli_flags_commingled_payout_across_entities(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: a Sparkry payout into the personal Chase must block the
    filing via REQ-BNO-CHK-007 — even when filing the ``sparkry`` entity, the
    check reads every non-rejected receipt so both commingling directions are
    caught (REQ-BNO-007)."""
    db = tmp_path / "commingled.db"
    _make_db(
        db,
        [
            tx(
                id="substack-payout",
                entity="sparkry",
                direction="transfer",
                amount="343.00",
                tax_category="SUBSCRIPTION_INCOME",
                payment_method="Chase ****3894",
                description="Substack payout",
            )
        ],
        accounts=[("Chase ****3894", "personal"), ("Chase ****0001", "sparkry")],
    )
    rc = main(["--entity", "sparkry", "--period", "2026-07", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "FAIL" in out and "REQ-BNO-CHK-007" in out
    assert "substack-payout" in out


def test_cli_commingling_passes_when_account_matches(tmp_path: Path) -> None:
    """Same revenue on the Sparkry business account → no commingling flag."""
    db = tmp_path / "clean-acct.db"
    _make_db(
        db,
        [
            tx(
                id="ok-payout",
                entity="sparkry",
                direction="transfer",
                amount="343.00",
                tax_category="SUBSCRIPTION_INCOME",
                payment_method="Chase ****0001",
                description="Substack payout",
            )
        ],
        accounts=[("Chase ****3894", "personal"), ("Chase ****0001", "sparkry")],
    )
    assert main(["--entity", "sparkry", "--period", "2026-07", "--db", str(db)]) == 0


def test_build_account_entity_map_and_load_all_transactions(tmp_path: Path) -> None:
    """DB-boundary helpers: the account map is built from the account table and
    the all-entity loader returns every non-rejected row (both entities)."""
    db = tmp_path / "map.db"
    _make_db(
        db,
        [
            tx(id="s", entity="sparkry", payment_method="Chase ****0001"),
            tx(id="p", entity="personal", payment_method="Chase ****3894"),
            tx(id="gone", entity="sparkry", status="rejected", payment_method="Chase ****0001"),
        ],
        accounts=[("Chase ****3894", "personal"), ("Chase ****0001", "sparkry"), (None, "sparkry")],
    )
    amap = build_account_entity_map(db)
    assert amap == {"Chase ****3894": "personal", "Chase ****0001": "sparkry"}
    all_rows = load_all_transactions(db)
    ids = {r["id"] for r in all_rows}
    assert ids == {"s", "p"}  # rejected excluded; both entities present


def test_build_account_entity_map_missing_table_is_empty(tmp_path: Path) -> None:
    """A DB with no account table yields an empty map (no crash)."""
    db = tmp_path / "no-acct.db"
    _make_db(db, [tx()])  # no accounts arg → no account table
    assert build_account_entity_map(db) == {}


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


def test_cli_reads_wal_resident_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A row committed only to the WAL (not yet checkpointed) must be seen.

    Regression guard: opening with ``immutable=1`` would ignore the -wal file
    and miss this failing row, wrongly printing 'clear to file'.
    """
    db = tmp_path / "wal.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE transactions (
            id TEXT PRIMARY KEY, source TEXT, source_id TEXT, source_hash TEXT,
            date TEXT, description TEXT, amount NUMERIC, entity TEXT,
            direction TEXT, tax_category TEXT, status TEXT, confidence REAL,
            reimbursement_link TEXT, raw_data TEXT
        )"""
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # schema into main db
    conn.execute("PRAGMA wal_autocheckpoint=0")
    # A sign-vs-direction violation committed to the WAL only; writer stays open.
    conn.execute(
        "INSERT INTO transactions (id, source, source_hash, date, description,"
        " amount, entity, direction, tax_category, status, confidence,"
        " reimbursement_link, raw_data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("wal-bad", "stripe", "h", "2026-07-15", "x", "50.00", "sparkry",
         "expense", "SUPPLIES", "confirmed", 1.0, None, "{}"),
    )
    conn.commit()
    try:
        rc = main(["--entity", "sparkry", "--period", "2026-07", "--db", str(db)])
    finally:
        conn.close()
    out = capsys.readouterr().out
    assert rc != 0, out  # the WAL-resident violation must be seen
    assert "REQ-BNO-CHK-001" in out and "FAIL" in out


def test_period_type_is_exported() -> None:
    assert isinstance(parse_period("2026-Q1"), Period)
