"""Tests for the North American Builder Plus IUL balance importer.

REQ-IUL-001  Upsert the IUL Account (broker=north_american, type=other,
             entity=personal, tax_sheltered=True) and book a balance snapshot.
REQ-IUL-002  Book the SURRENDER value when provided; fall back to the
             accumulation value with a loud warning (overstates liquidation).
REQ-IUL-003  DRY-RUN default never writes; re-import is idempotent (dedup hash).
REQ-IUL-004  Decimal precision preserved end-to-end (no float corruption).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.north_american_iul import import_policy
from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import AccountType, Broker, Entity, IngestionStatus
from src.models.history import AccountBalanceSnapshot
from src.models.ingestion_log import IngestionLog
from src.models.plaid import (
    PlaidItem,  # noqa: F401 — Account FKs plaid_item; register for create_all
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


_POLICY = "NA-IUL-0001"
_AS_OF = date(2026, 6, 24)


def test_dry_run_writes_nothing(session: Session) -> None:
    """REQ-IUL-003: dry-run parses/validates but never touches the DB."""
    result = import_policy(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value=Decimal("450000.00"),
        dry_run=True,
    )
    assert result.errors == []
    assert session.scalars(select(Account)).all() == []
    assert session.scalars(select(AccountBalanceSnapshot)).all() == []


def test_apply_upserts_account_and_books_surrender_value(session: Session) -> None:
    """REQ-IUL-001 + REQ-IUL-002: account created, surrender value booked."""
    result = import_policy(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value=Decimal("450000.00"),
        accumulation_value=Decimal("466928.72"),
        beneficiary="Amy Sparks",
        account_name="North American Builder Plus IUL4 — Travis",
        dry_run=False,
        session=session,
    )
    assert result.imported == 1
    assert result.errors == []

    acct = session.scalars(select(Account)).one()
    assert acct.broker == Broker.NORTH_AMERICAN.value
    assert acct.account_number == _POLICY
    assert acct.account_type == AccountType.OTHER.value
    assert acct.entity == Entity.PERSONAL.value
    assert acct.tax_sheltered is True
    assert acct.beneficiary == "Amy Sparks"

    snap = session.scalars(select(AccountBalanceSnapshot)).one()
    assert snap.account_id == acct.id
    assert snap.as_of == _AS_OF
    # REQ-IUL-002: surrender value wins over accumulation value.
    assert snap.balance == Decimal("450000.00")


def test_falls_back_to_accumulation_with_warning(session: Session) -> None:
    """REQ-IUL-002: no surrender value → book accumulation, warn loudly."""
    result = import_policy(
        policy_number=_POLICY,
        as_of=_AS_OF,
        accumulation_value=Decimal("466928.72"),
        dry_run=False,
        session=session,
    )
    assert result.imported == 1
    snap = session.scalars(select(AccountBalanceSnapshot)).one()
    assert snap.balance == Decimal("466928.72")
    assert any("surrender" in w.lower() for w in result.warnings)


def test_requires_some_value(session: Session) -> None:
    """Neither surrender nor accumulation → a clean error, no write."""
    result = import_policy(
        policy_number=_POLICY, as_of=_AS_OF, dry_run=False, session=session
    )
    assert result.imported == 0
    assert result.errors
    assert session.scalars(select(AccountBalanceSnapshot)).all() == []


def test_reimport_is_idempotent(session: Session) -> None:
    """REQ-IUL-003: same policy/date/balance re-import is a dedup no-op."""
    kw = dict(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value=Decimal("450000.00"),
        dry_run=False,
        session=session,
    )
    first = import_policy(**kw)
    second = import_policy(**kw)
    assert first.imported == 1
    assert second.imported == 0
    assert second.dup_skipped == 1
    assert len(session.scalars(select(AccountBalanceSnapshot)).all()) == 1
    # The account is reused, not duplicated.
    assert len(session.scalars(select(Account)).all()) == 1


def test_cloud_posts_surrender_value_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-IUL-005: cloud port POSTs one snapshot row with the surrender value."""
    from src.adapters import north_american_iul as mod

    captured: dict = {}

    def _fake_post(payload, source):  # type: ignore[no-untyped-def]
        captured["payload"] = payload
        captured["source"] = source
        return {"ok": True}

    monkeypatch.setattr(mod, "post_to_wealth", _fake_post)
    result = mod.import_policy_cloud(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value="$256,564.03",
        accumulation_value="$466,928.72",
        account_name="North American Builder Plus IUL4 — Travis",
    )
    assert result.imported == 1
    assert result.errors == []
    rows = captured["payload"]["rows"]
    assert len(rows) == 1
    # Surrender value wins, quantized to cents, posted as a string.
    assert rows[0]["balance"] == "256564.03"
    assert rows[0]["as_of"] == "2026-06-24"
    assert rows[0]["source"] == "north_american_iul"


def test_string_input_preserves_precision(session: Session) -> None:
    """REQ-IUL-004: '$466,928.72' parses to the exact Decimal, no float drift."""
    result = import_policy(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value="$466,928.72",
        dry_run=False,
        session=session,
    )
    snap = session.scalars(select(AccountBalanceSnapshot)).one()
    assert snap.balance == Decimal("466928.72")
    assert result.errors == []


# ---------------------------------------------------------------------------
# REQ-FIX-WLT-009: notes merge — human text survives, machine block replaced once
# ---------------------------------------------------------------------------


def test_notes_written_as_delimited_machine_block(session: Session) -> None:
    """The importer records its figures inside a `--- [na_iul auto ...]` block."""
    import_policy(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value=Decimal("100.00"),
        accumulation_value=Decimal("110.00"),
        dry_run=False,
        session=session,
    )
    acct = session.scalars(select(Account)).one()
    assert acct.notes is not None
    assert acct.notes.startswith("--- [na_iul auto ")
    assert "accumulation=110.00" in acct.notes


def test_human_notes_survive_repeated_imports(session: Session) -> None:
    """REQ-FIX-WLT-009: operator free text is preserved; machine block replaced once."""
    # First import creates the account + machine block.
    import_policy(
        policy_number=_POLICY,
        as_of=date(2026, 1, 1),
        surrender_value=Decimal("100.00"),
        accumulation_value=Decimal("110.00"),
        dry_run=False,
        session=session,
    )
    acct = session.scalars(select(Account)).one()

    # Operator prepends free text above the machine block.
    human = "REVIEW: confirm beneficiary designation with Amy before EOY"
    acct.notes = f"{human}\n{acct.notes}"
    session.commit()

    # Second import (new date + value → new snapshot, so _upsert_account runs).
    import_policy(
        policy_number=_POLICY,
        as_of=date(2026, 2, 1),
        surrender_value=Decimal("120.00"),
        accumulation_value=Decimal("130.00"),
        dry_run=False,
        session=session,
    )
    session.refresh(acct)

    assert acct.notes is not None
    # Human text intact.
    assert acct.notes.startswith(human)
    # Exactly one machine block — the old one was replaced, not appended.
    assert acct.notes.count("--- [na_iul auto") == 1
    # And it reflects the LATEST import.
    assert "accumulation=130.00" in acct.notes
    assert "accumulation=110.00" not in acct.notes


# ---------------------------------------------------------------------------
# REQ-FIX-WLT-007: cloud import writes a local IngestionLog row
# ---------------------------------------------------------------------------


def test_cloud_import_writes_ingestion_log_on_success(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful cloud push writes one IngestionLog row (source-tagged)."""
    from src.adapters import north_american_iul as mod

    monkeypatch.setattr(mod, "post_to_wealth", lambda payload, source: {"ok": True})
    result = mod.import_policy_cloud(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value="$100.00",
        session=session,
    )
    assert result.imported == 1
    assert result.errors == []

    logs = session.scalars(select(IngestionLog)).all()
    assert len(logs) == 1
    assert logs[0].source == "wealth_cloud:north_american_iul"
    assert logs[0].status == IngestionStatus.SUCCESS.value


def test_cloud_import_writes_ingestion_log_on_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed cloud push still writes one IngestionLog row (status=error)."""
    from src.adapters import north_american_iul as mod
    from src.adapters._shared.wealth_client import WealthTransportError

    def _boom(payload, source):  # type: ignore[no-untyped-def]
        raise WealthTransportError("connection refused")

    monkeypatch.setattr(mod, "post_to_wealth", _boom)
    result = mod.import_policy_cloud(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value="$100.00",
        session=session,
    )
    assert result.imported == 0
    assert result.errors

    logs = session.scalars(select(IngestionLog)).all()
    assert len(logs) == 1
    assert logs[0].source == "wealth_cloud:north_american_iul"
    assert logs[0].status == IngestionStatus.FAILURE.value


def test_cloud_import_without_session_skips_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat: no session → no IngestionLog attempted, still returns result."""
    from src.adapters import north_american_iul as mod

    monkeypatch.setattr(mod, "post_to_wealth", lambda payload, source: {"ok": True})
    result = mod.import_policy_cloud(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value="$100.00",
    )
    assert result.imported == 1
    assert result.errors == []
