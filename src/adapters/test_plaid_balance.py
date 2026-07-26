"""Tests for src/adapters/plaid_balance.py — REQ-026.

Uses an in-memory SQLite (same pattern as test_xlsx_savings_plan) so the
fixtures can FK against ``account``, ``plaid_item``, ``audit_events``, etc.

The Plaid SDK client is mocked. We use ``src/adapters/fixtures/plaid/fixtures.py``
to load realistic JSON-shaped responses.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from plaid.exceptions import ApiException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.adapters.fixtures.plaid.fixtures import make_response_from_fixture
from src.adapters.plaid_balance import (
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
    stamped as the run date (today), never derived from that field, and the
    snapshot writes normally even when the underlying value is unchanged from
    a prior day (freshness is a digest-level concern, not a write-time one)."""
    item = _make_item(session, institution_name="Chase")
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001", account_number="1111"
    )
    client = _mock_client_returning("accounts_get_null_last_updated")

    result = sync_one_item(session, item, client=client)
    session.commit()

    assert result.status == "ok"
    assert result.accounts_processed == 1
    snap = session.query(PlaidAccountBalanceSnapshot).one()
    assert snap.snapshot_date == date.today()
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


def test_fresh_balances_collected_for_register_scope_including_unmapped(
    session: Session,
) -> None:
    """REQ-PC-B2: register-scope Items collect a payload row for EVERY USD
    account with a balance — mapped or not (the D1's own account table decides
    what maps endpoint-side)."""
    item = _make_item(session, institution_name="Chase")
    _make_account(
        session, item=item, plaid_account_id="plaid_acct_chase_checking_0001",
        account_number="1111",
    )
    # savings + card left unmapped locally.
    client = _mock_client_returning("accounts_balance_get_mixed")

    result = sync_one_item(session, item, client=client)
    session.commit()

    assert result.scope == "register"
    assert len(result.fresh_balances) == 3
    by_id = {row["plaid_account_id"]: row for row in result.fresh_balances}
    checking = by_id["plaid_acct_chase_checking_0001"]
    assert checking["current_balance"] == "4523.18"
    assert checking["available_balance"] == "4523.18"
    assert checking["plaid_account_type"] == "depository"
    assert checking["plaid_account_subtype"] == "checking"
    assert checking["iso_currency_code"] == "USD"
    assert checking["snapshot_date"] == date.today().isoformat()
    assert isinstance(checking["fetched_at"], int)
    assert checking["fetched_at"] > 1_500_000_000_000  # epoch MILLISECONDS
    # 2dp string formatting (12500.0 → "12500.00") — mirrors toFixed(2).
    assert by_id["plaid_acct_chase_savings_0002"]["current_balance"] == "12500.00"
    # Credit card stays positive-as-returned (liability negation is a D1
    # READ-side rule, never applied at write time).
    assert by_id["plaid_acct_chase_card_0003"]["current_balance"] == "583.45"
    # Register behavior unchanged: 1 snapshot + 2 expected_account rows.
    assert session.query(PlaidAccountBalanceSnapshot).count() == 1
    assert session.query(ExpectedAccount).count() == 2


def test_fresh_balances_still_collected_on_same_day_duplicate(session: Session) -> None:
    """A same-day re-run collides on UNIQUE locally (savepoint rollback) but
    must STILL collect the payload row — D1's conditional upsert on fetched_at
    wants the freshest value."""
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

    assert len(r1.fresh_balances) == 3
    assert len(r2.fresh_balances) == 3


def test_non_usd_account_not_collected_for_push(session: Session) -> None:
    """Non-USD accounts are skipped for the D1 push too (endpoint convention)."""
    item = _make_wealth_item(session, institution_name="ForeignBank")
    client = _mock_client_returning("accounts_balance_get_non_usd")

    result = sync_one_item(session, item, client=client)
    assert result.accounts_skipped_non_usd == 1
    assert result.fresh_balances == []


# ── push_fresh_balances (REQ-PC-B2) ──────────────────────────────────────────


def _batch_with(rows_by_item: dict[str, list[dict[str, Any]]]) -> Any:
    from src.adapters.plaid_balance import BatchResult, ItemSyncResult

    batch = BatchResult(dry_run=False)
    for name, rows in rows_by_item.items():
        batch.items.append(
            ItemSyncResult(
                item_id=f"id-{name}", institution_name=name, status="ok",
                fresh_balances=rows,
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
        return {"ok": True}

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
        calls.append(len(payload["snapshots"]))
        return {"ok": True}

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
        return {"ok": True}

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
    push = push_fresh_balances(batch, session=session, post=lambda p, s: {"ok": True})

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
    assert push.failed is False
