"""Tests for src/adapters/plaid_balance.py — REQ-026.

Uses an in-memory SQLite (same pattern as test_xlsx_savings_plan) so the
fixtures can FK against ``account``, ``plaid_item``, ``audit_events``, etc.

The Plaid SDK client is mocked. We use ``src/adapters/fixtures/plaid/fixtures.py``
to load realistic JSON-shaped responses.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from plaid.exceptions import ApiException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.adapters.fixtures.plaid.fixtures import make_response_from_fixture
from src.adapters.plaid_balance import (
    _fresh_balance_row,
    sync_all_active,
    sync_one_item,
)
from src.models.base import Base
from src.models.brokerage import Account
from src.models.history import ExpectedAccount
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem
from src.utils.plaid_crypto import encrypt_token

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _set_enc_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Every test runs under a fresh Fernet key so encryption works."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PLAID_TOKEN_ENC_KEY", key)
    return key


@pytest.fixture
def session() -> Session:
    """Fresh in-memory SQLite with FK enforcement."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # Ensure plaid + history + brokerage + audit_events models are registered.
    from src.models import (
        audit_event,  # noqa: F401
        plaid,  # noqa: F401
    )

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _make_item(session: Session, *, institution_name: str = "Chase") -> PlaidItem:
    item = PlaidItem(
        item_id=f"plaid_test_{institution_name.lower()}",
        institution_id="ins_3",
        institution_name=institution_name,
        access_token_encrypted=encrypt_token("access-sandbox-test-token"),
    )
    session.add(item)
    session.commit()
    return item


def _make_account(
    session: Session,
    *,
    item: PlaidItem,
    plaid_account_id: str,
    account_number: str = "9999",
) -> Account:
    acct = Account(
        broker="schwab",  # any valid broker enum; not relevant for balance sync
        account_number=account_number,
        account_type="taxable",
        entity="personal",
        plaid_item_id=item.id,
        plaid_account_id=plaid_account_id,
    )
    session.add(acct)
    session.commit()
    return acct


def _mock_client_returning(fixture_name: str) -> MagicMock:
    client = MagicMock()
    client.accounts_get.return_value = make_response_from_fixture(fixture_name)
    return client


def _mock_client_raising(error_code: str) -> MagicMock:
    """Return a client whose accounts_get raises a Plaid ApiException."""
    client = MagicMock()
    exc = ApiException(status=400, reason="Test")
    exc.body = json.dumps({"error_code": error_code, "error_message": f"mock {error_code}"})
    client.accounts_get.side_effect = exc
    return client


# ── Happy paths ──────────────────────────────────────────────────────────────


def test_successful_sync_writes_snapshot_for_mapped_accounts(session: Session) -> None:
    """REQ-026: mapped Plaid accounts produce one snapshot row each."""
    item = _make_item(session, institution_name="Chase")
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_savings_0002", account_number="2222"
    )
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_card_0003", account_number="3333"
    )

    client = _mock_client_returning("accounts_balance_get_mixed")
    result = sync_one_item(session, item, client=client)
    session.commit()

    assert result.status == "ok"
    assert result.accounts_processed == 3
    assert result.accounts_failed == 0
    snaps = session.query(PlaidAccountBalanceSnapshot).all()
    assert len(snaps) == 3
    # Credit card current balance is stored as-returned (positive = debt).
    card_snap = next(s for s in snaps if s.plaid_account_type == "credit")
    assert card_snap.current_balance == Decimal("583.45")
    # Item bookkeeping updated.
    session.refresh(item)
    assert item.last_sync_status == "ok"
    assert item.last_error is None
    assert item.last_sync_at is not None


def test_double_run_is_idempotent(session: Session) -> None:
    """REQ-026: UNIQUE(account_id, snapshot_date) absorbs the second run."""
    item = _make_item(session)
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    client = _mock_client_returning("accounts_balance_get_mixed")

    # First run.
    r1 = sync_one_item(session, item, client=client)
    session.commit()
    assert r1.accounts_processed >= 1
    first_count = session.query(PlaidAccountBalanceSnapshot).count()

    # Second run, same day — collisions absorbed.
    r2 = sync_one_item(session, item, client=client)
    session.commit()
    assert session.query(PlaidAccountBalanceSnapshot).count() == first_count
    # processed counter is incremented but failed stays zero.
    assert r2.accounts_failed == 0
    assert r2.status == "ok"


def test_unmapped_plaid_account_upserts_expected_account(session: Session) -> None:
    """REQ-026: a Plaid account with no mapped Account row → ExpectedAccount with status='unconfirmed'."""
    item = _make_item(session, institution_name="Chase")
    # No Account rows mapped at all.
    client = _mock_client_returning("accounts_balance_get_mixed")

    result = sync_one_item(session, item, client=client)
    session.commit()

    assert result.accounts_processed == 0
    assert result.accounts_failed == 0
    assert result.accounts_skipped_unmapped == 3  # all three accounts in the fixture
    expected = session.query(ExpectedAccount).all()
    assert len(expected) == 3
    assert all(e.status == "unconfirmed" for e in expected)
    assert all(e.source == "plaid" for e in expected)
    assert all(e.institution == "Chase" for e in expected)


def test_unmapped_account_is_idempotent_on_double_run(session: Session) -> None:
    """Second run with same unmapped accounts does not duplicate ExpectedAccount rows."""
    item = _make_item(session)
    client = _mock_client_returning("accounts_balance_get_mixed")

    sync_one_item(session, item, client=client)
    session.commit()
    first_expected = session.query(ExpectedAccount).count()

    sync_one_item(session, item, client=client)
    session.commit()
    assert session.query(ExpectedAccount).count() == first_expected


def test_non_usd_account_skipped_with_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """REQ-026: non-USD account is skipped and counted, no snapshot written."""
    import logging

    item = _make_item(session, institution_name="ForeignBank")
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_cad_0001", account_number="cad-x"
    )
    client = _mock_client_returning("accounts_balance_get_non_usd")

    caplog.set_level(logging.WARNING, logger="src.adapters.plaid_balance")
    result = sync_one_item(session, item, client=client)
    session.commit()

    assert result.accounts_skipped_non_usd == 1
    assert result.accounts_processed == 0
    assert session.query(PlaidAccountBalanceSnapshot).count() == 0
    assert any("non-USD" in rec.message for rec in caplog.records)


# ── Error isolation paths ────────────────────────────────────────────────────


def test_terminal_error_marks_item_error(session: Session) -> None:
    """REQ-026: ITEM_LOGIN_REQUIRED → no snapshots, item.last_sync_status='error'."""
    item = _make_item(session)
    client = _mock_client_raising("ITEM_LOGIN_REQUIRED")

    result = sync_one_item(session, item, client=client)
    session.commit()

    session.refresh(item)
    assert result.status == "error"
    assert result.error_code == "ITEM_LOGIN_REQUIRED"
    assert result.retryable is False
    assert item.last_sync_status == "error"
    assert item.last_error == "ITEM_LOGIN_REQUIRED"
    # No snapshots written.
    assert session.query(PlaidAccountBalanceSnapshot).count() == 0
    # IngestionLog records the failure.
    log = session.query(IngestionLog).filter_by(source=f"plaid_balance:{item.institution_name}").one()
    assert log.status == "failure"
    assert log.error_detail == "ITEM_LOGIN_REQUIRED"


def test_retryable_error_after_exhaustion_marks_error_retryable(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RATE_LIMIT_EXCEEDED on all retries → status=error, retryable=True (transient)."""
    item = _make_item(session)
    client = _mock_client_raising("RATE_LIMIT_EXCEEDED")
    # Cut backoff to ~zero so the test is fast.
    monkeypatch.setattr(
        "src.adapters.plaid_client.RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0)
    )

    result = sync_one_item(session, item, client=client)
    session.commit()

    session.refresh(item)
    assert result.status == "error"
    assert result.error_code == "RATE_LIMIT_EXCEEDED"
    assert result.retryable is True
    assert item.last_sync_status == "error"
    log = session.query(IngestionLog).filter_by(source=f"plaid_balance:{item.institution_name}").one()
    assert log.retryable is True


def test_institution_down_status_distinct_from_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INSTITUTION_DOWN → item.last_sync_status='institution_down' (distinct UI badge)."""
    item = _make_item(session)
    client = _mock_client_raising("INSTITUTION_DOWN")
    monkeypatch.setattr(
        "src.adapters.plaid_client.RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0)
    )

    result = sync_one_item(session, item, client=client)
    session.commit()
    session.refresh(item)
    assert item.last_sync_status == "institution_down"
    assert result.status == "institution_down"


def test_per_account_exception_isolation(session: Session) -> None:
    """One bad Plaid account does not block snapshot for sibling accounts."""
    item = _make_item(session)
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_savings_0002", account_number="2222"
    )

    # Build a response where the first account has a poisoned to_dict() (raises).
    resp = make_response_from_fixture("accounts_balance_get_mixed")
    poisoned = resp.accounts[0]

    def _explode() -> dict[str, Any]:
        raise RuntimeError("simulated per-account failure")

    poisoned.to_dict = _explode  # type: ignore[attr-defined]

    client = MagicMock()
    client.accounts_get.return_value = resp

    result = sync_one_item(session, item, client=client)
    session.commit()

    # 1 failure (the poisoned account), 1 success (savings), 1 unmapped (card).
    assert result.accounts_failed == 1
    assert result.accounts_processed == 1
    assert result.accounts_skipped_unmapped == 1
    snaps = session.query(PlaidAccountBalanceSnapshot).all()
    assert len(snaps) == 1
    assert snaps[0].plaid_account_subtype == "savings"


def test_per_item_exception_isolation(session: Session) -> None:
    """Item A fails terminally; Item B still gets processed in the batch."""
    _make_item(session, institution_name="Chase")
    item_b = _make_item(session, institution_name="Vanguard")
    _make_account(
        session,
        item=item_b,
        plaid_account_id="plaid_acct_vanguard_brk_0001",
        account_number="brk-v1",
    )
    _make_account(
        session,
        item=item_b,
        plaid_account_id="plaid_acct_vanguard_ira_0002",
        account_number="ira-v2",
    )

    # Per-Item branching: the client's first call (alphabetically: Chase) raises a
    # terminal Plaid error; the second call (Vanguard) returns a normal response.
    # sync_all_active orders Items by query, which on SQLite happens to be insertion order.

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        def accounts_get(self, _req: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                exc = ApiException(status=400, reason="?")
                exc.body = json.dumps(
                    {"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "x"}
                )
                raise exc
            return make_response_from_fixture("accounts_balance_get_brokerage")

    client = _Client()
    batch = sync_all_active(session, client=client, dry_run=False)

    assert len(batch.items) == 2
    chase_result = next(r for r in batch.items if r.institution_name == "Chase")
    vanguard_result = next(r for r in batch.items if r.institution_name == "Vanguard")
    assert chase_result.status == "error"
    assert chase_result.error_code == "ITEM_LOGIN_REQUIRED"
    assert vanguard_result.status == "ok"
    assert vanguard_result.accounts_processed == 2
    # Snapshots only for the Vanguard Item.
    assert session.query(PlaidAccountBalanceSnapshot).count() == 2


def test_invalid_ciphertext_treated_as_terminal(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the stored token can't be decrypted (key rotated, sentinel REVOKED, etc),
    the Item is marked terminal-error so the user re-links."""
    item = _make_item(session)
    # Overwrite token with garbage so decryption fails.
    item.access_token_encrypted = "REVOKED"
    session.commit()

    client = MagicMock()
    result = sync_one_item(session, item, client=client)
    session.commit()

    assert result.status == "error"
    assert result.error_code == "INVALID_ACCESS_TOKEN"
    # We did NOT call Plaid because we couldn't decrypt.
    client.accounts_get.assert_not_called()


def test_ingestion_log_written_per_item_run(session: Session) -> None:
    """REQ-026: every Item run writes exactly one IngestionLog row, even on failure."""
    item = _make_item(session, institution_name="Chase")
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    client = _mock_client_returning("accounts_balance_get_mixed")

    sync_one_item(session, item, client=client)
    session.commit()
    logs = session.query(IngestionLog).filter(IngestionLog.source.like("plaid_balance:%")).all()
    assert len(logs) == 1
    assert logs[0].source == "plaid_balance:Chase"
    # Run with no mapped accounts → still exactly one log row total.
    item2 = _make_item(session, institution_name="Vanguard")
    sync_one_item(session, item2, client=_mock_client_returning("accounts_balance_get_brokerage"))
    session.commit()
    logs = session.query(IngestionLog).filter(IngestionLog.source.like("plaid_balance:%")).all()
    assert len(logs) == 2


# ── Batch driver semantics ───────────────────────────────────────────────────


def test_sync_all_active_dry_run_rolls_back(session: Session) -> None:
    """sync_all_active(dry_run=True) executes the flow but rolls back."""
    item = _make_item(session)
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    client = _mock_client_returning("accounts_balance_get_mixed")

    batch = sync_all_active(session, client=client, dry_run=True)
    assert batch.dry_run is True
    # Dry-run rolls back: no snapshots persisted.
    assert session.query(PlaidAccountBalanceSnapshot).count() == 0
    # The IngestionLog ALSO rolls back — we don't want noisy "fake run" entries.
    assert session.query(IngestionLog).filter(
        IngestionLog.source.like("plaid_balance:%")
    ).count() == 0


def test_sync_all_active_writes_when_apply(session: Session) -> None:
    """sync_all_active(dry_run=False) commits."""
    item = _make_item(session)
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    client = _mock_client_returning("accounts_balance_get_mixed")

    batch = sync_all_active(session, client=client, dry_run=False)
    assert batch.dry_run is False
    assert session.query(PlaidAccountBalanceSnapshot).count() == 1


def test_disconnected_items_skipped(session: Session) -> None:
    """Items with status='disconnected' are not synced."""
    item = _make_item(session)
    item.status = "disconnected"
    session.commit()
    client = MagicMock()

    batch = sync_all_active(session, client=client, dry_run=False)
    assert batch.items == []
    client.accounts_get.assert_not_called()


# ── REQ-FIX-PLD-001: /accounts/get request construction ─────────────────────


def test_accounts_request_returns_accounts_get_request_with_token() -> None:
    from plaid.model.accounts_get_request import AccountsGetRequest

    from src.adapters.plaid_balance import _accounts_request

    req = _accounts_request("access-sandbox-test-token")
    assert isinstance(req, AccountsGetRequest)
    assert req.access_token == "access-sandbox-test-token"


def test_sync_calls_accounts_get_never_accounts_balance_get(session: Session) -> None:
    item = _make_item(session, institution_name="Chase")
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    client = _mock_client_returning("accounts_balance_get_mixed")

    sync_one_item(session, item, client=client)

    client.accounts_get.assert_called_once()
    # accounts_balance_get is a MagicMock auto-attribute — assert it was never
    # invoked (would auto-create if accessed, so we check call count instead).
    assert client.accounts_balance_get.call_count == 0


def test_accounts_get_with_null_last_updated_datetime_stamps_run_date(
    session: Session,
) -> None:
    """REQ-FIX-PLD-001: /accounts/get's balances.last_updated_datetime is
    typically None (non-Capital-One institutions) — snapshot_date must still be
    stamped as the run date, never derived from that field, and the snapshot
    writes normally even when the underlying value is unchanged from a prior
    day (freshness is a digest-level concern, not a write-time one).

    P3-r3m: "run date" means the UTC day of ``pulled_at``, so pin pulled_at
    rather than comparing against the host's local ``date.today()``."""
    item = _make_item(session, institution_name="Chase")
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    client = _mock_client_returning("accounts_get_null_last_updated")

    pulled_at = datetime(2026, 12, 31, 23, 59, 0)  # 1 minute before UTC midnight
    result = sync_one_item(session, item, client=client, pulled_at=pulled_at)
    session.commit()

    assert result.status == "ok"
    assert result.accounts_processed == 1
    snap = session.query(PlaidAccountBalanceSnapshot).one()
    assert snap.snapshot_date == date(2026, 12, 31)
    assert snap.current_balance == Decimal("4523.18")


# ── REQ-FIX-PLD-004: dead placeholder items excluded from rotation ──────────


def test_placeholder_item_excluded_from_sync_rotation(session: Session) -> None:
    """A placeholder item (item_id LIKE 'placeholder_%') is never synced, even
    if its status is still 'active' (query-parity fix, independent of the
    one-time data-fix script that flips it to disconnected)."""
    item = PlaidItem(
        item_id="placeholder_abc123",
        institution_id="ins_9",
        institution_name="Dead Placeholder",
        access_token_encrypted=encrypt_token("access-sandbox-test-token"),
        status="active",
    )
    session.add(item)
    session.commit()
    client = MagicMock()

    batch = sync_all_active(session, client=client, dry_run=False)

    assert batch.items == []
    client.accounts_get.assert_not_called()


# ── REQ-FIX-PLD-005: unmapped-account detail + ignore-list ──────────────────


def test_unmapped_accounts_logged_with_name_mask_subtype(session: Session) -> None:
    item = _make_item(session, institution_name="Chase")
    client = _mock_client_returning("accounts_balance_get_mixed")

    result = sync_one_item(session, item, client=client)
    session.commit()

    assert result.accounts_skipped_unmapped == 3
    assert len(result.unmapped) == 3
    assert "Chase Total Checking ·0123· checking" in result.unmapped
    assert "Chase Sapphire Preferred ·4567· credit card" in result.unmapped
    log = session.query(IngestionLog).filter_by(
        source=f"plaid_balance:{item.institution_name}"
    ).one()
    assert log.error_detail is not None
    assert "unmapped_skipped=3" in log.error_detail
    assert "Chase Total Checking ·0123· checking" in log.error_detail


def test_ignored_expected_account_not_counted_as_unmapped(session: Session) -> None:
    """REQ-FIX-PLD-005: an account flipped to status='ignored' stops counting
    as unmapped on subsequent syncs."""
    item = _make_item(session, institution_name="Chase")
    session.add(
        ExpectedAccount(
            institution="Chase",
            account_name="Chase Total Checking",
            last_4="0123",
            status="ignored",
            source="plaid",
        )
    )
    session.commit()
    client = _mock_client_returning("accounts_balance_get_mixed")

    result = sync_one_item(session, item, client=client)
    session.commit()

    # 2 of the 3 fixture accounts remain unmapped; the ignored one is excluded.
    assert result.accounts_skipped_unmapped == 2
    assert not any("Chase Total Checking" in u for u in result.unmapped)
    # The ignored row itself is untouched (still 'ignored', not overwritten).
    ignored = session.query(ExpectedAccount).filter_by(account_name="Chase Total Checking").one()
    assert ignored.status == "ignored"


# ── REQ-PC-B1/B2: item scope + fresh-balance collection + D1 push ────────────


def _make_wealth_item(session: Session, *, institution_name: str = "ETRADE") -> PlaidItem:
    item = PlaidItem(
        item_id=f"plaid_wealth_{institution_name.lower()}",
        institution_id="ins_129473",
        institution_name=institution_name,
        access_token_encrypted=encrypt_token("access-sandbox-test-token"),
        scope="wealth",
    )
    session.add(item)
    session.commit()
    return item


def test_wealth_scope_item_never_writes_register_rows(session: Session) -> None:
    """REQ-PC-B1: a wealth-scope Item produces NO snapshot rows and NO
    expected_account rows — only in-memory fresh_balances for the D1 push."""
    item = _make_wealth_item(session)
    client = _mock_client_returning("accounts_balance_get_mixed")

    result = sync_one_item(session, item, client=client)
    session.commit()

    assert result.scope == "wealth"
    assert result.status == "ok"
    assert result.accounts_processed == 3
    assert result.accounts_failed == 0
    assert result.accounts_skipped_unmapped == 0
    assert len(result.fresh_balances) == 3
    # The two register side-effect tables stay EMPTY.
    assert session.query(PlaidAccountBalanceSnapshot).count() == 0
    assert session.query(ExpectedAccount).count() == 0
    # IngestionLog still records the run, with the scope in the detail.
    log = session.query(IngestionLog).filter_by(
        source=f"plaid_balance:{item.institution_name}"
    ).one()
    assert log.error_detail is not None
    assert "scope=wealth" in log.error_detail
    assert "fresh_balances=3" in log.error_detail
    # Item bookkeeping still updates (staleness surface).
    session.refresh(item)
    assert item.last_sync_status == "ok"


def test_wealth_scope_null_balance_counts_failed(session: Session) -> None:
    """A wealth account with no current balance is a failure, mirroring the
    register path's null-balance rule."""
    from types import SimpleNamespace

    item = _make_wealth_item(session)
    acct = SimpleNamespace(
        account_id="p_wealth_null",
        type="investment",
        subtype="ira",
        balances=SimpleNamespace(current=None, available=None, iso_currency_code="USD"),
    )
    client = MagicMock()
    client.accounts_get.return_value = SimpleNamespace(accounts=[acct])

    result = sync_one_item(session, item, client=client)
    assert result.accounts_failed == 1
    assert result.accounts_processed == 0
    assert result.fresh_balances == []


def test_register_scope_collects_no_fresh_balances(session: Session) -> None:
    """P0-r3a: register-scope Items collect NOTHING for the D1 push.

    D1 has never contained the box's register accounts — Plaid account_ids are
    per-Item, and D1's own Chase balances come from its own migrated Item — so
    a register row could only ever produce an unmapped skip there. Register
    balances stay in the local snapshot table, exactly as pre-consolidation.
    """
    item = _make_item(session, institution_name="Chase")
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001",
        account_number="1111",
    )
    # savings + card left unmapped locally.
    client = _mock_client_returning("accounts_balance_get_mixed")

    pulled_at = datetime(2026, 7, 25, 23, 45, 0)
    result = sync_one_item(session, item, client=client, pulled_at=pulled_at)
    session.commit()

    assert result.scope == "register"
    assert result.fresh_balances == []
    # Register behavior unchanged: 1 snapshot + 2 expected_account rows.
    assert session.query(PlaidAccountBalanceSnapshot).count() == 1
    assert session.query(ExpectedAccount).count() == 2


def test_wealth_scope_fresh_balance_row_shape(session: Session) -> None:
    """REQ-PC-B2: the payload row a wealth Item contributes to the A1 push."""
    item = _make_wealth_item(session, institution_name="Schwab")
    client = _mock_client_returning("accounts_balance_get_mixed")

    # P2-003: pin pulled_at explicitly (rather than relying on the ambient
    # system clock via the default _utcnow()) so this assertion is
    # deterministic regardless of the test runner's local timezone or the
    # wall-clock instant it happens to run at — and so it actually exercises
    # the invariant under test: snapshot_date is the UTC day of pulled_at,
    # not the host's local date.today().
    pulled_at = datetime(2026, 7, 25, 23, 45, 0)
    result = sync_one_item(session, item, client=client, pulled_at=pulled_at)
    session.commit()

    assert len(result.fresh_balances) == 3
    by_id = {row["plaid_account_id"]: row for row in result.fresh_balances}
    checking = by_id["plaid_acct_chase_checking_0001"]
    assert checking["current_balance"] == "4523.18"
    assert checking["available_balance"] == "4523.18"
    assert checking["plaid_account_type"] == "depository"
    assert checking["plaid_account_subtype"] == "checking"
    assert checking["iso_currency_code"] == "USD"
    assert checking["snapshot_date"] == "2026-07-25"  # UTC day of pulled_at
    assert isinstance(checking["fetched_at"], int)
    assert checking["fetched_at"] > 1_500_000_000_000  # epoch MILLISECONDS
    # 2dp string formatting (12500.0 → "12500.00") — mirrors toFixed(2).
    assert by_id["plaid_acct_chase_savings_0002"]["current_balance"] == "12500.00"
    # Credit card stays positive-as-returned (liability negation is a D1
    # READ-side rule, never applied at write time).
    assert by_id["plaid_acct_chase_card_0003"]["current_balance"] == "583.45"


def test_fresh_balance_row_snapshot_date_is_utc_day_of_fetched_at(session: Session) -> None:
    """P2-003: snapshot_date must be the UTC day of fetched_at/pulled_at, not
    the box's system-local date.today() — the D1 (account_id, snapshot_date)
    key, the fresher-wins upsert, and the drift baseline's
    `snapshot_date < ?` query all depend on a UTC day key. Pin pulled_at just
    before UTC midnight so a local-date bug (date.today() picking the NEXT
    local day in a timezone ahead of UTC, or the PREVIOUS local day behind
    UTC) would be caught regardless of the host's timezone."""
    item = _make_wealth_item(session, institution_name="Schwab")
    client = _mock_client_returning("accounts_balance_get_mixed")

    pulled_at = datetime(2026, 12, 31, 23, 59, 0)  # 1 minute before UTC midnight
    result = sync_one_item(session, item, client=client, pulled_at=pulled_at)
    session.commit()

    row = next(
        r for r in result.fresh_balances
        if r["plaid_account_id"] == "plaid_acct_chase_checking_0001"
    )
    assert row["snapshot_date"] == "2026-12-31"
    assert row["fetched_at"] == int(pulled_at.replace(tzinfo=UTC).timestamp() * 1000)


def test_local_snapshot_date_matches_pushed_snapshot_date(session: Session) -> None:
    """P3-r3m: the local snapshot row and the D1 payload row must key off the
    SAME instant. Both now derive from ``pulled_at.date()``; neither may fall
    back to the host's local ``date.today()``, or a run straddling local
    midnight would write two different day keys for one pull."""
    register_item = _make_item(session, institution_name="Chase")
    _make_account(
        session, item=register_item, plaid_account_id="plaid_acct_chase_checking_0001",
        account_number="1111",
    )
    wealth_item = _make_wealth_item(session, institution_name="Schwab")
    client = _mock_client_returning("accounts_balance_get_mixed")

    pulled_at = datetime(2026, 12, 31, 23, 59, 0)
    sync_one_item(session, register_item, client=client, pulled_at=pulled_at)
    wealth = sync_one_item(session, wealth_item, client=client, pulled_at=pulled_at)
    session.commit()

    snap = session.query(PlaidAccountBalanceSnapshot).one()
    assert snap.snapshot_date == date(2026, 12, 31)
    assert {r["snapshot_date"] for r in wealth.fresh_balances} == {"2026-12-31"}


def test_wealth_fresh_balances_still_collected_on_same_day_rerun(session: Session) -> None:
    """A same-day re-run must STILL collect the payload rows — D1's conditional
    upsert on fetched_at wants the freshest value on every run."""
    item = _make_wealth_item(session, institution_name="Schwab")
    client = _mock_client_returning("accounts_balance_get_mixed")

    r1 = sync_one_item(session, item, client=client)
    session.commit()
    r2 = sync_one_item(session, item, client=client)
    session.commit()

    assert len(r1.fresh_balances) == 3
    assert len(r2.fresh_balances) == 3


def test_register_same_day_duplicate_stays_idempotent(session: Session) -> None:
    """The register path's UNIQUE(account_id, snapshot_date) savepoint-rollback
    idempotency is unaffected by the wealth-only push change."""
    item = _make_item(session)
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001",
        account_number="1111",
    )
    client = _mock_client_returning("accounts_balance_get_mixed")

    r1 = sync_one_item(session, item, client=client)
    session.commit()
    r2 = sync_one_item(session, item, client=client)
    session.commit()

    assert session.query(PlaidAccountBalanceSnapshot).count() == 1
    assert r1.accounts_failed == 0
    assert r2.accounts_failed == 0
    assert r1.fresh_balances == []
    assert r2.fresh_balances == []


def test_non_usd_account_not_collected_for_push(session: Session) -> None:
    """Non-USD accounts are skipped for the D1 push too (endpoint convention)."""
    item = _make_wealth_item(session, institution_name="ForeignBank")
    client = _mock_client_returning("accounts_balance_get_non_usd")

    result = sync_one_item(session, item, client=client)
    assert result.accounts_skipped_non_usd == 1
    assert result.fresh_balances == []


# ── push_fresh_balances (REQ-PC-B2) ──────────────────────────────────────────


def _batch_with(
    rows_by_item: dict[str, list[dict[str, Any]]], *, scope: str = "wealth"
) -> Any:
    """Build a BatchResult. Defaults to wealth scope — P0-r3a: only wealth-scope
    Items are eligible for the D1 push at all."""
    from src.adapters.plaid_balance import BatchResult, ItemSyncResult

    batch = BatchResult(dry_run=False)
    for name, rows in rows_by_item.items():
        batch.items.append(
            ItemSyncResult(
                item_id=f"id-{name}", institution_name=name, status="ok",
                scope=scope, fresh_balances=rows,
            )
        )
    return batch


def _row(i: int) -> dict[str, Any]:
    return {"plaid_account_id": f"acct-{i}", "current_balance": "1.00"}


def test_push_posts_snapshots_payload_with_correct_slug(session: Session) -> None:
    from src.adapters.plaid_balance import push_fresh_balances

    calls: list[tuple[dict[str, Any], str]] = []

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        calls.append((payload, source))
        n = len(payload["snapshots"])
        return {"records_written": n, "records_processed": n}

    batch = _batch_with({"Chase": [_row(1), _row(2)]})
    push = push_fresh_balances(batch, post=_post)

    assert push.total_pushed == 2
    assert push.failed is False
    assert len(calls) == 1
    payload, source = calls[0]
    assert source == "plaid-balance"
    assert list(payload.keys()) == ["snapshots"]
    assert payload["snapshots"] == [_row(1), _row(2)]


def test_push_chunks_at_batch_cap(session: Session) -> None:
    from src.adapters.plaid_balance import push_fresh_balances

    calls: list[int] = []

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        n = len(payload["snapshots"])
        calls.append(n)
        return {"records_written": n, "records_processed": n}

    batch = _batch_with({"Big": [_row(i) for i in range(450)]})
    push = push_fresh_balances(batch, post=_post)

    assert calls == [200, 200, 50]
    assert push.total_pushed == 450


def test_push_per_item_isolation_and_failure_flag(session: Session) -> None:
    """One Item's failed push never blocks the next Item's push; the batch is
    flagged failed so the CLI exits non-zero (the staleness alert)."""
    from src.adapters._shared.wealth_client import WealthHTTPError
    from src.adapters.plaid_balance import push_fresh_balances

    calls: list[str] = []

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        first = payload["snapshots"][0]["plaid_account_id"]
        calls.append(first)
        if first == "acct-1":
            raise WealthHTTPError(500, "boom")
        n = len(payload["snapshots"])
        return {"records_written": n, "records_processed": n}

    batch = _batch_with({"Failing": [_row(1)], "Healthy": [_row(2)]})
    push = push_fresh_balances(batch, session=session, post=_post)

    assert calls == ["acct-1", "acct-2"]
    assert push.failed is True
    failing = next(p for p in push.items if p.institution_name == "Failing")
    healthy = next(p for p in push.items if p.institution_name == "Healthy")
    assert failing.error is not None and "WealthHTTPError" in failing.error
    assert failing.pushed == 0
    assert healthy.error is None
    assert healthy.pushed == 1
    # Partial failure recorded on the local delivery-health log.
    log = session.query(IngestionLog).filter_by(source="wealth_cloud:plaid_balance").one()
    assert log.status == "partial_failure"
    assert log.records_processed == 1
    assert log.records_failed == 1


def test_push_success_writes_success_ingestion_log(session: Session) -> None:
    from src.adapters.plaid_balance import push_fresh_balances

    batch = _batch_with({"Chase": [_row(1)]})
    push = push_fresh_balances(
        batch,
        session=session,
        post=lambda p, s: {
            "records_written": len(p["snapshots"]),
            "records_processed": len(p["snapshots"]),
        },
    )

    assert push.failed is False
    log = session.query(IngestionLog).filter_by(source="wealth_cloud:plaid_balance").one()
    assert log.status == "success"
    assert log.records_processed == 1


def test_push_with_no_rows_never_posts(session: Session) -> None:
    from src.adapters.plaid_balance import push_fresh_balances

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        raise AssertionError("post should not be called for empty batches")

    batch = _batch_with({"Empty": []})
    push = push_fresh_balances(batch, post=_post)
    assert push.total_pushed == 0


def test_push_all_skipped_unmapped_trips_failure_not_silent_success(
    session: Session,
) -> None:
    """P1-b2r/P1-002: a 200 response where the endpoint resolved ZERO
    plaid_account_ids (e.g. a freshly re-linked Item — P0-001) must NOT look
    like success. `pushed` (rows sent) is nonzero but `records_written` is 0
    — the push must flag `failed`, not silently report a clean run."""
    from src.adapters.plaid_balance import push_fresh_balances

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        n = len(payload["snapshots"])
        return {"records_written": 0, "records_skipped_unmapped": n, "records_failed": 0}

    batch = _batch_with({"FreshlyRelinked": [_row(1), _row(2)]})
    push = push_fresh_balances(batch, session=session, post=_post)

    assert push.total_pushed == 2
    assert push.failed is True
    item = push.items[0]
    assert item.records_written == 0
    assert item.records_skipped_unmapped == 2
    assert item.error is not None
    assert "unmapped" in item.error
    log = session.query(IngestionLog).filter_by(source="wealth_cloud:plaid_balance").one()
    assert log.status != "success"


def test_push_idempotent_resend_does_not_trip_failure(session: Session) -> None:
    """P2-001: A1's `records_written` excludes idempotent no-ops (the
    conditional upsert didn't change anything) but `records_processed`
    includes them. An exact re-push of an already-written batch — every row a
    legitimate no-op — must NOT be reported as a D1 write failure."""
    from src.adapters.plaid_balance import push_fresh_balances

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        n = len(payload["snapshots"])
        # Every row was accepted but stale/idempotent — the conditional
        # upsert made no changes, so records_written is 0 even though
        # records_processed (accepted rows, written OR no-op) is nonzero.
        return {"records_written": 0, "records_processed": n, "records_failed": 0}

    batch = _batch_with({"ReSent": [_row(1), _row(2)]})
    push = push_fresh_balances(batch, session=session, post=_post)

    assert push.total_pushed == 2
    assert push.failed is False
    item = push.items[0]
    assert item.records_written == 0
    assert item.records_processed == 2
    assert item.error is None
    log = session.query(IngestionLog).filter_by(source="wealth_cloud:plaid_balance").one()
    assert log.status == "success"


def test_push_ambiguous_plaid_account_id_trips_failure(session: Session) -> None:
    """P2-002: `records_skipped_ambiguous` (multiple D1 accounts share a
    plaid_account_id) is a data-integrity problem — it must trip the push
    failure flag even when other rows in the same batch wrote cleanly."""
    from src.adapters.plaid_balance import push_fresh_balances

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {"records_written": 1, "records_processed": 1, "records_skipped_ambiguous": 1}

    batch = _batch_with({"Ambiguous": [_row(1), _row(2)]})
    push = push_fresh_balances(batch, session=session, post=_post)

    assert push.failed is True
    item = push.items[0]
    assert item.records_skipped_ambiguous == 1
    assert item.error is not None
    assert "ambiguous" in item.error


def test_push_partial_unmapped_is_informational_not_a_failure(
    session: Session,
) -> None:
    """Cutover policy 2026-07-26: wealth Items legitimately carry sub-accounts
    D1 never mapped (E*TRADE: 4 of 5), matching the retired wealth sync's own
    skip-and-count behavior. PARTIAL unmapped = counted + logged, exit clean."""
    from src.adapters.plaid_balance import push_fresh_balances

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {
            "records_written": 1,
            "records_processed": 1,
            "records_skipped_unmapped": 4,
        }

    batch = _batch_with({"Mixed": [_row(1)]})
    push = push_fresh_balances(batch, session=session, post=_post)

    assert push.failed is False
    item = push.items[0]
    assert item.records_skipped_unmapped == 4
    assert item.error is None


def test_push_wholly_unmapped_item_is_a_failure(session: Session) -> None:
    """The mapping-broke signature: EVERY deliverable row skipped-unmapped and
    nothing resolved — must trip the non-zero exit / OnFailure alert."""
    from src.adapters.plaid_balance import push_fresh_balances

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {
            "records_written": 0,
            "records_processed": 0,
            "records_skipped_unmapped": len(payload["snapshots"]),
        }

    batch = _batch_with({"Broken": [_row(1)]})
    push = push_fresh_balances(batch, session=session, post=_post)

    assert push.failed is True
    item = push.items[0]
    assert item.error is not None and "ALL" in item.error


def test_push_skips_register_scope_items_entirely(session: Session) -> None:
    """P0-r3a: a mixed batch pushes ONLY the wealth-scope Item's rows. The
    register Item is absent from the payload AND from the push result, and the
    run is clean (exit 0) — its balances belong in the local snapshot table."""
    from src.adapters.plaid_balance import BatchResult, ItemSyncResult, push_fresh_balances

    posted: list[list[dict[str, Any]]] = []

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        posted.append(payload["snapshots"])
        n = len(payload["snapshots"])
        return {"records_written": n, "records_processed": n}

    batch = BatchResult(dry_run=False)
    batch.items.append(
        ItemSyncResult(
            item_id="id-chase", institution_name="Chase", status="ok",
            scope="register", fresh_balances=[_row(1)],
        )
    )
    batch.items.append(
        ItemSyncResult(
            item_id="id-schwab", institution_name="Schwab", status="ok",
            scope="wealth", fresh_balances=[_row(2)],
        )
    )
    push = push_fresh_balances(batch, session=session, post=_post)

    assert push.failed is False
    assert [p.institution_name for p in push.items] == ["Schwab"]
    assert push.total_pushed == 1
    # Exactly one POST, carrying only the wealth row.
    assert posted == [[_row(2)]]
    assert all(r["plaid_account_id"] != "acct-1" for chunk in posted for r in chunk)
    log = session.query(IngestionLog).filter_by(source="wealth_cloud:plaid_balance").one()
    assert log.status == "success"
    assert log.records_processed == 1


def test_push_all_register_batch_never_posts(session: Session) -> None:
    """P0-r3a: with no wealth-scope Items there is no push at all — no HTTP
    call, no push results, no delivery-health log row, and a clean exit."""
    from src.adapters.plaid_balance import push_fresh_balances

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        raise AssertionError("register-scope balances must never be POSTed to D1")

    batch = _batch_with({"Chase": [_row(1)], "Amex": [_row(2)]}, scope="register")
    push = push_fresh_balances(batch, session=session, post=_post)

    assert push.items == []
    assert push.total_pushed == 0
    assert push.failed is False
    assert session.query(IngestionLog).filter_by(
        source="wealth_cloud:plaid_balance"
    ).count() == 0


def test_push_skipped_non_usd_is_informational_not_a_failure(session: Session) -> None:
    """P1-r3c-2: ``records_skipped_non_usd`` never trips a failure — even when
    it accounts for every row in the batch (aligning with the CRM-side
    decision). Only unmapped/ambiguous/failed rows are errors."""
    from src.adapters.plaid_balance import push_fresh_balances

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        n = len(payload["snapshots"])
        return {"records_written": 0, "records_processed": 0, "records_skipped_non_usd": n}

    batch = _batch_with({"ForeignBank": [_row(1), _row(2)]})
    push = push_fresh_balances(batch, session=session, post=_post)

    assert push.failed is False
    assert push.items[0].records_skipped_non_usd == 2
    assert push.items[0].error is None
    log = session.query(IngestionLog).filter_by(source="wealth_cloud:plaid_balance").one()
    assert log.status == "success"


def test_push_endpoint_reported_records_failed_trips_failure(session: Session) -> None:
    """The endpoint's own `records_failed` count (per-row DB errors on its
    side) must also trip the push failure flag, not just transport errors."""
    from src.adapters.plaid_balance import push_fresh_balances

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {"records_written": 1, "records_failed": 1}

    batch = _batch_with({"Flaky": [_row(1), _row(2)]})
    push = push_fresh_balances(batch, post=_post)

    assert push.failed is True
    assert "1 failed row" in (push.items[0].error or "")


# ── P2-006: A1 golden-row cross-repo contract fixture ───────────────────────
#
# Mirrors the P1-xct pattern already established for A2
# (test_plaid_investments.py::test_contract_fixture_matches_committed_json /
# tests/fixtures/plaid-consolidation-contract/{01,02}-*.json): a payload built
# from the REAL box builder (`_fresh_balance_row`), committed verbatim to a
# fixture file both repos load, so a change to the builder's output shape
# fails a test in THIS repo instead of silently drifting from what
# sparkry-crm-plaidcons's endpoint test suite happens to hand-write.
#
# Covers the two cases the artifact review flagged as under-tested on the A1
# side: a plain USD depository row (exercising the float->2dp-string money
# path) and a credit/loan-type row (exercising the read-side liability-
# negation predicate, which keys on `plaid_account_type` ∈ {credit, loan}).

_CONTRACT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "tests"
    / "fixtures"
    / "plaid-consolidation-contract"
)


def _contract_balance_account(
    *,
    account_id: str,
    acct_type: str,
    subtype: str | None,
    current: Any,
    available: Any = None,
) -> Any:
    balances = SimpleNamespace(current=current, available=available, iso_currency_code="USD")
    return SimpleNamespace(account_id=account_id, type=acct_type, subtype=subtype, balances=balances)


def _build_contract_balance_snapshots() -> dict[str, Any]:
    """Reconstruct the exact payload the 03-balance-chunk.json fixture was
    generated from — a plain USD depository row (money as a Python float,
    exercising the ROUND_HALF_UP-to-2dp string path) and a credit/loan row
    (the liability-negation case)."""
    pulled_at = datetime(2026, 7, 25, 4, 20, tzinfo=UTC).replace(tzinfo=None)
    checking = _contract_balance_account(
        account_id="plaid_acct_contract_checking",
        acct_type="depository",
        subtype="checking",
        current=1234.5,
        available=1000,
    )
    credit = _contract_balance_account(
        account_id="plaid_acct_contract_credit",
        acct_type="credit",
        subtype="credit card",
        current="500.005",  # string input — HALF_UP quantize to "500.01"
        available=None,
    )
    rows = [
        _fresh_balance_row(checking, pulled_at=pulled_at),
        _fresh_balance_row(credit, pulled_at=pulled_at),
    ]
    return {"snapshots": rows}


def test_balance_contract_fixture_matches_committed_json() -> None:
    """The committed A1 golden-row fixture is byte-identical to a fresh
    `_fresh_balance_row` build.

    If this fails, either `_fresh_balance_row`'s output shape changed (update
    BOTH this repo's fixture AND sparkry-crm-plaidcons's copy — see the
    fixture dir's README) or this helper drifted from the documented
    generation recipe.
    """
    payload = _build_contract_balance_snapshots()
    committed = json.loads((_CONTRACT_FIXTURE_DIR / "03-balance-chunk.json").read_text())
    assert payload == committed


# ---------------------------------------------------------------------------
# Reliability-audit P1 (2026-07-27): the per-account IntegrityError absorb is
# scoped to the idempotency UNIQUE — any other integrity failure is a FAILED
# write, not a processed account.
# ---------------------------------------------------------------------------


def test_non_unique_integrity_error_counts_failed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    import src.adapters.plaid_balance as mod

    item = _make_item(session)
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    client = _mock_client_returning("accounts_balance_get_mixed")

    def _boom(*args: object, **kwargs: object) -> None:
        raise SAIntegrityError(
            "INSERT ...", {}, Exception("CHECK constraint failed: ck_plaid_bal_snap_account_type")
        )

    monkeypatch.setattr(mod, "_process_plaid_account", _boom)
    result = sync_one_item(session, item, client=client)
    session.commit()
    assert result.accounts_failed >= 1
    assert result.accounts_processed == 0


def test_unique_snapshot_collision_still_absorbed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    import src.adapters.plaid_balance as mod

    item = _make_item(session)
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    client = _mock_client_returning("accounts_balance_get_mixed")

    def _dup(*args: object, **kwargs: object) -> None:
        raise SAIntegrityError(
            "INSERT ...",
            {},
            Exception(
                "UNIQUE constraint failed: plaid_account_balance_snapshot.account_id,"
                " plaid_account_balance_snapshot.snapshot_date"
            ),
        )

    monkeypatch.setattr(mod, "_process_plaid_account", _dup)
    result = sync_one_item(session, item, client=client)
    session.commit()
    assert result.accounts_failed == 0
    assert result.accounts_processed >= 1


# ── REQ-FIX-PLD-007 — incident 2026-08-17: superseded same-institution Item ─
#
# Two "Bank of America" Items were active on the box (the original, plus a
# fresh Link that minted new plaid_account_ids). After the D1 dossier was
# relinked to the NEW ids on 2026-08-08, the OLD Item's single row went
# wholly-unmapped and the sync paged daily for 9 days with the message
# "Bank of America: D1 skipped ALL 1 row(s) as unmapped" — which named neither
# Item nor account, so the operator could not tell WHICH of the two same-name
# Items to retire. The contract (wholly-unmapped Item → page, siblings still
# push, exit 1) is unchanged; the page must now carry the exact ids and, when
# a healthy same-institution sibling exists, the retire command.


def _wealth_item_result(
    local_id: str,
    institution_name: str,
    rows: list[dict[str, Any]],
    *,
    plaid_item_id: str = "",
    labels: dict[str, str] | None = None,
) -> Any:
    from src.adapters.plaid_balance import ItemSyncResult

    return ItemSyncResult(
        item_id=local_id,
        institution_name=institution_name,
        status="ok",
        scope="wealth",
        fresh_balances=rows,
        plaid_item_id=plaid_item_id,
        fresh_labels=labels or {},
    )


def _post_unmapped_only_for(unmapped_ids: set[str]) -> Any:
    """Endpoint stub: rows whose plaid_account_id is in ``unmapped_ids`` are
    skipped-unmapped; everything else is written."""

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        rows = payload["snapshots"]
        n_unmapped = sum(1 for r in rows if r["plaid_account_id"] in unmapped_ids)
        n_ok = len(rows) - n_unmapped
        return {
            "records_processed": n_ok,
            "records_written": n_ok,
            "records_skipped_unmapped": n_unmapped,
            "records_failed": 0,
        }

    return _post


def test_push_one_wholly_unmapped_item_among_many_pages_but_pushes_the_rest(
    session: Session,
) -> None:
    """One wholly-unmapped Item in a multi-Item batch: every OTHER Item still
    pushes and resolves, the batch is flagged failed (exit 1 → OnFailure page),
    the local delivery-health log is partial_failure, and the error names the
    exact local Item id, Plaid item_id, and unmapped plaid_account_id(s) with
    their name·mask·subtype labels."""
    from src.adapters.plaid_balance import BatchResult, push_fresh_balances

    batch = BatchResult(dry_run=False)
    batch.items.append(
        _wealth_item_result(
            "id-boa-old",
            "Bank of America",
            [{"plaid_account_id": "N0qbo-old-ascent", "current_balance": "3416.18"}],
            plaid_item_id="qna7-old",
            labels={"N0qbo-old-ascent": "Atmos Rewards Ascent Visa Signature ·8196· credit card"},
        )
    )
    batch.items.append(
        _wealth_item_result(
            "id-boa-new",
            "Bank of America",
            [
                {"plaid_account_id": "PLxKw-new-summit", "current_balance": "4043.10"},
                {"plaid_account_id": "1BAnK-new-ascent", "current_balance": "3416.18"},
            ],
            plaid_item_id="yBm4-new",
        )
    )
    batch.items.append(
        _wealth_item_result("id-vanguard", "Vanguard", [_row(1), _row(2)], plaid_item_id="ewP9")
    )

    push = push_fresh_balances(
        batch, session=session, post=_post_unmapped_only_for({"N0qbo-old-ascent"})
    )

    # Contract: siblings unaffected, batch flagged.
    assert push.total_pushed == 5
    assert push.failed is True
    by_id = {p.item_id: p for p in push.items}
    assert by_id["id-boa-new"].error is None
    assert by_id["id-boa-new"].records_processed == 2
    assert by_id["id-vanguard"].error is None
    assert by_id["id-vanguard"].records_processed == 2
    old = by_id["id-boa-old"]
    assert old.error is not None
    assert "ALL 1 row(s) as unmapped" in old.error
    # Actionable identity — never just the (ambiguous) institution name.
    assert "id-boa-old" in old.error
    assert "qna7-old" in old.error
    assert "N0qbo-old-ascent" in old.error
    assert "Atmos Rewards Ascent Visa Signature ·8196· credit card" in old.error
    # The healthy same-institution sibling makes this the superseded-Item
    # signature: the page carries the retire command for THIS Item's local id.
    assert "SUPERSEDED" in old.error
    assert "id-boa-new" in old.error
    assert "/api/plaid/disconnect/id-boa-old" in old.error
    # The retire hint must never point at the healthy sibling.
    assert "/api/plaid/disconnect/id-boa-new" not in old.error

    log = session.query(IngestionLog).filter_by(source="wealth_cloud:plaid_balance").one()
    assert log.status == "partial_failure"
    assert log.records_processed == 5
    assert log.records_failed == 1
    assert log.error_detail is not None
    assert "id-boa-old" in log.error_detail
    assert "/api/plaid/disconnect/id-boa-old" in log.error_detail


def test_push_wholly_unmapped_item_without_healthy_sibling_has_no_superseded_hint(
    session: Session,
) -> None:
    """A wholly-unmapped Item whose institution has NO other resolving Item is
    the genuine mapping-broke case: still pages, still names ids, but must not
    tell the operator to retire it — there is nothing superseding it."""
    from src.adapters.plaid_balance import BatchResult, push_fresh_balances

    batch = BatchResult(dry_run=False)
    batch.items.append(
        _wealth_item_result(
            "id-schwab", "Charles Schwab", [_row(1)], plaid_item_id="ZgEm",
            labels={"acct-1": "Schwab One ·1234· brokerage"},
        )
    )
    batch.items.append(
        _wealth_item_result("id-vanguard", "Vanguard", [_row(2)], plaid_item_id="ewP9")
    )

    push = push_fresh_balances(
        batch, session=session, post=_post_unmapped_only_for({"acct-1"})
    )

    assert push.failed is True
    schwab = next(p for p in push.items if p.item_id == "id-schwab")
    assert schwab.error is not None
    assert "id-schwab" in schwab.error and "ZgEm" in schwab.error
    assert "acct-1" in schwab.error and "Schwab One ·1234· brokerage" in schwab.error
    assert "SUPERSEDED" not in schwab.error
    assert "/api/plaid/disconnect/" not in schwab.error
    vanguard = next(p for p in push.items if p.item_id == "id-vanguard")
    assert vanguard.error is None


def test_push_superseded_hint_ignores_sibling_that_also_failed_to_resolve(
    session: Session,
) -> None:
    """Two same-institution Items BOTH wholly-unmapped is not supersession —
    neither may be told to retire in favour of the other."""
    from src.adapters.plaid_balance import BatchResult, push_fresh_balances

    batch = BatchResult(dry_run=False)
    batch.items.append(_wealth_item_result("id-a", "Bank of America", [_row(1)]))
    batch.items.append(_wealth_item_result("id-b", "Bank of America", [_row(2)]))

    push = push_fresh_balances(
        batch, session=session, post=_post_unmapped_only_for({"acct-1", "acct-2"})
    )

    assert push.failed is True
    for p in push.items:
        assert p.error is not None
        assert "SUPERSEDED" not in p.error
        assert "/api/plaid/disconnect/" not in p.error


def test_wealth_scope_sync_records_plaid_item_id_and_account_labels(
    session: Session,
) -> None:
    """The per-Item sync result must carry what the push needs to page
    actionably: the Plaid item_id and a name·mask·subtype label per collected
    plaid_account_id (register scope already had this via ``unmapped``)."""
    item = _make_wealth_item(session, institution_name="Chase")
    client = _mock_client_returning("accounts_balance_get_mixed")

    result = sync_one_item(session, item, client=client)
    session.commit()

    assert result.plaid_item_id == item.item_id
    assert set(result.fresh_labels) == {r["plaid_account_id"] for r in result.fresh_balances}
    assert (
        result.fresh_labels["plaid_acct_chase_checking_0001"]
        == "Chase Total Checking ·0123· checking"
    )
    assert (
        result.fresh_labels["plaid_acct_chase_card_0003"]
        == "Chase Sapphire Preferred ·4567· credit card"
    )
