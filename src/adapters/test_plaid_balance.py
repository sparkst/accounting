"""Tests for src/adapters/plaid_balance.py — REQ-026.

Uses an in-memory SQLite (same pattern as test_xlsx_savings_plan) so the
fixtures can FK against ``account``, ``plaid_item``, ``audit_events``, etc.

The Plaid SDK client is mocked. We use ``src/adapters/fixtures/plaid/fixtures.py``
to load realistic JSON-shaped responses.
"""

from __future__ import annotations

import json
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
from src.models.history import ExpectedAccount  # noqa: F401 — register on metadata
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
    client.accounts_balance_get.return_value = make_response_from_fixture(fixture_name)
    return client


def _mock_client_raising(error_code: str) -> MagicMock:
    """Return a client whose accounts_balance_get raises a Plaid ApiException."""
    client = MagicMock()
    exc = ApiException(status=400, reason="Test")
    exc.body = json.dumps({"error_code": error_code, "error_message": f"mock {error_code}"})
    client.accounts_balance_get.side_effect = exc
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
    client.accounts_balance_get.return_value = resp

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

        def accounts_balance_get(self, _req: Any) -> Any:
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
    client.accounts_balance_get.assert_not_called()


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
    client.accounts_balance_get.assert_not_called()
