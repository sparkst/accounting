"""Tests for scripts/import_wealth_plaid_items.py — REQ-PC-B4.

Covers: STDIN-only entry, Fernet encryption via the existing plaid_crypto,
scope='wealth' + status='active' insertion, entity-mode audit rows,
idempotency on item_id, DRY-RUN default, per-row error isolation, and the
no-token-leakage rule for error output.
"""

from __future__ import annotations

import io
import json
from collections.abc import Generator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from scripts.import_wealth_plaid_items import (
    ACTOR,
    _read_stdin,
    import_items,
    main,
)
from src.models.audit_event import ENTITY_TYPE_PLAID_ITEM, AuditEvent
from src.models.base import Base
from src.models.plaid import PlaidItem
from src.utils.plaid_crypto import decrypt_token, encrypt_token


@pytest.fixture(autouse=True)
def _set_enc_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PLAID_TOKEN_ENC_KEY", key)
    return key


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


_TOKEN = "access-production-supersecret-token-abc"  # noqa: S105 — test fixture


def _entry(item_id: str = "wealth_item_1") -> dict[str, str]:
    return {
        "item_id": item_id,
        "institution_id": "ins_129473",
        "institution_name": "E*TRADE from Morgan Stanley",
        "access_token_plain": _TOKEN,
    }


def test_apply_inserts_wealth_item_encrypted_with_audit(db: Session) -> None:
    result = import_items(db, [_entry()], apply=True)

    assert result.errors == []
    assert len(result.imported) == 1
    item = db.query(PlaidItem).filter_by(item_id="wealth_item_1").one()
    assert item.scope == "wealth"
    assert item.status == "active"
    # Token stored Fernet-encrypted, decryptable, never plaintext.
    assert item.access_token_encrypted != _TOKEN
    assert decrypt_token(item.access_token_encrypted) == _TOKEN
    # Entity-mode audit row.
    audit = db.query(AuditEvent).filter_by(
        entity_id=item.id, entity_type=ENTITY_TYPE_PLAID_ITEM
    ).one()
    assert audit.field_changed == "connect"
    assert audit.changed_by == ACTOR
    assert _TOKEN not in (audit.new_value or "")


def test_dry_run_default_rolls_back(db: Session) -> None:
    result = import_items(db, [_entry()])  # apply defaults False

    assert len(result.imported) == 1  # reported…
    assert db.query(PlaidItem).count() == 0  # …but not committed
    assert db.query(AuditEvent).count() == 0


def test_idempotent_on_item_id(db: Session) -> None:
    """A second run skips the existing Item and never touches its row."""
    import_items(db, [_entry()], apply=True)
    original = db.query(PlaidItem).filter_by(item_id="wealth_item_1").one()
    original_token = original.access_token_encrypted

    entry2 = _entry()
    entry2["access_token_plain"] = "access-production-DIFFERENT"  # noqa: S105
    result = import_items(db, [entry2], apply=True)

    assert result.imported == []
    assert len(result.skipped_existing) == 1
    assert db.query(PlaidItem).count() == 1
    refreshed = db.query(PlaidItem).filter_by(item_id="wealth_item_1").one()
    assert refreshed.access_token_encrypted == original_token  # untouched


def test_existing_register_item_is_skipped_not_rescoped(db: Session) -> None:
    """An item_id already present as register scope is left alone (loudly)."""
    db.add(
        PlaidItem(
            item_id="wealth_item_1",
            institution_id="ins_3",
            institution_name="Chase",
            access_token_encrypted=encrypt_token("tok"),
            scope="register",
        )
    )
    db.commit()

    result = import_items(db, [_entry()], apply=True)
    assert result.imported == []
    assert len(result.skipped_existing) == 1
    assert "scope=register" in result.skipped_existing[0]
    assert db.query(PlaidItem).one().scope == "register"


def test_per_row_isolation_and_no_token_leakage(db: Session) -> None:
    """A malformed entry is counted and reported WITHOUT leaking any token
    material; healthy siblings still import."""
    bad = {"item_id": "x", "access_token_plain": _TOKEN}  # missing fields
    good = _entry("wealth_item_2")

    result = import_items(db, [bad, good], apply=True)

    assert len(result.imported) == 1
    assert len(result.errors) == 1
    assert _TOKEN not in " ".join(result.errors)
    assert db.query(PlaidItem).filter_by(item_id="wealth_item_2").count() == 1


@pytest.mark.parametrize(
    "entry",
    [
        "not-a-dict",
        {},
        {"item_id": "", "institution_id": "i", "institution_name": "n",
         "access_token_plain": "t"},
        {"item_id": "a", "institution_id": "i", "institution_name": "n",
         "access_token_plain": "   "},
    ],
)
def test_malformed_entries_are_errors(db: Session, entry: object) -> None:
    result = import_items(db, [entry], apply=True)
    assert len(result.errors) == 1
    assert db.query(PlaidItem).count() == 0


def test_read_stdin_rejects_non_list() -> None:
    with pytest.raises(ValueError, match="JSON list"):
        _read_stdin(io.StringIO(json.dumps({"item_id": "x"})))


def test_main_refuses_interactive_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """STDIN-only entry: an interactive terminal is refused before any DB or
    JSON work (tokens must be piped, never typed)."""

    class _FakeTty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _FakeTty())
    assert main([]) == 2


def test_main_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    assert main([], stdin=io.StringIO("not json")) == 2
