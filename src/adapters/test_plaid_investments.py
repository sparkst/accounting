"""Tests for src/adapters/plaid_investments.py — REQ-PC-B3.

Wealth-scope Items' ``/investments/holdings/get`` → A2 push. The Plaid SDK
client and the wealth POST are both injected; no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from plaid.exceptions import ApiException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.adapters.plaid_investments import (
    WEALTH_HOLDINGS_INGEST_SOURCE,
    build_holdings_payload,
    chunk_holdings_payload,
    sync_all_wealth,
    sync_one_item,
)
from src.models.base import Base
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidItem
from src.utils.plaid_crypto import encrypt_token

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _set_enc_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PLAID_TOKEN_ENC_KEY", key)
    return key


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    from src.models import audit_event, plaid  # noqa: F401

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _make_item(
    session: Session,
    *,
    institution_name: str = "ETRADE",
    scope: str = "wealth",
    status: str = "active",
    item_id: str | None = None,
) -> PlaidItem:
    item = PlaidItem(
        item_id=item_id or f"plaid_inv_{institution_name.lower()}",
        institution_id="ins_129473",
        institution_name=institution_name,
        access_token_encrypted=encrypt_token("access-sandbox-test-token"),
        scope=scope,
        status=status,
    )
    session.add(item)
    session.commit()
    return item


def _security(security_id: str = "sec_1", ticker: str = "VTI") -> SimpleNamespace:
    d = {
        "security_id": security_id,
        "isin": None,
        "cusip": "922908769",
        "ticker_symbol": ticker,
        "name": f"{ticker} ETF",
        "type": "etf",
        "close_price": 280.5,
        "iso_currency_code": "USD",
    }
    return SimpleNamespace(**d, to_dict=lambda _d=d: _d)


def _holding(
    account_id: str = "p_acct_inv_1", security_id: str = "sec_1"
) -> SimpleNamespace:
    d = {
        "account_id": account_id,
        "security_id": security_id,
        "institution_price": 280.5,
        "institution_value": 2805.0,
        "cost_basis": 2000.0,
        "quantity": 10.0,
        "iso_currency_code": "USD",
    }
    return SimpleNamespace(**d, to_dict=lambda _d=d: _d)


def _mock_client(
    securities: list[Any] | None = None, holdings: list[Any] | None = None
) -> MagicMock:
    client = MagicMock()
    client.investments_holdings_get.return_value = SimpleNamespace(
        securities=securities if securities is not None else [_security()],
        holdings=holdings if holdings is not None else [_holding()],
        accounts=[],
    )
    return client


def _mock_client_raising(error_code: str) -> MagicMock:
    client = MagicMock()
    exc = ApiException(status=400, reason="Test")
    exc.body = json.dumps({"error_code": error_code, "error_message": f"mock {error_code}"})
    client.investments_holdings_get.side_effect = exc
    return client


# ── Payload contract (A2) ────────────────────────────────────────────────────


def test_build_holdings_payload_shape(session: Session) -> None:
    item = _make_item(session)
    resp = SimpleNamespace(
        securities=[_security()], holdings=[_holding()], accounts=[]
    )
    pulled_at = datetime(2026, 7, 25, 4, 20, 0)

    payload = build_holdings_payload(item, resp, pulled_at=pulled_at)

    assert payload["item_id"] == item.item_id
    assert payload["institution_name"] == "ETRADE"
    assert payload["pulled_at"] == pulled_at.replace(tzinfo=UTC).isoformat()
    assert payload["fetched_at"] == int(
        pulled_at.replace(tzinfo=UTC).timestamp() * 1000
    )
    assert payload["securities"][0]["security_id"] == "sec_1"
    assert payload["securities"][0]["ticker_symbol"] == "VTI"
    # Holdings carry the PLAID account id — the endpoint resolves it.
    assert payload["holdings"][0]["account_id"] == "p_acct_inv_1"
    assert payload["holdings"][0]["quantity"] == 10.0
    # Payload must be JSON-serializable (dates from the SDK stringified).
    json.dumps(payload)


def test_payload_json_safe_with_native_dates(session: Session) -> None:
    """SDK to_dict() output can carry date objects — payload must still dump."""
    from datetime import date

    item = _make_item(session)
    d = {"security_id": "sec_d", "close_price_as_of": date(2026, 7, 24)}
    sec = SimpleNamespace(**d, to_dict=lambda _d=d: _d)
    resp = SimpleNamespace(securities=[sec], holdings=[], accounts=[])

    payload = build_holdings_payload(item, resp, pulled_at=datetime(2026, 7, 25))
    json.dumps(payload)
    assert payload["securities"][0]["close_price_as_of"] == "2026-07-24"


# ── Happy path ───────────────────────────────────────────────────────────────


def test_apply_pushes_and_logs(session: Session) -> None:
    item = _make_item(session)
    client = _mock_client()
    posts: list[tuple[dict[str, Any], str]] = []

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        posts.append((payload, source))
        return {"holdings_processed": len(payload["holdings"])}

    batch = sync_all_wealth(session, client=client, dry_run=False, post=_post)

    assert len(batch.items) == 1
    r = batch.items[0]
    assert r.status == "ok"
    assert r.pushed is True
    assert r.securities == 1
    assert r.holdings == 1
    assert len(posts) == 1
    assert posts[0][1] == WEALTH_HOLDINGS_INGEST_SOURCE
    log = session.query(IngestionLog).filter_by(
        source=f"plaid_investments:{item.institution_name}"
    ).one()
    assert log.status == "success"
    assert log.records_processed == 1


def test_dry_run_fetches_but_never_posts_and_rolls_back(session: Session) -> None:
    _make_item(session)
    client = _mock_client()

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        raise AssertionError("dry-run must never POST")

    batch = sync_all_wealth(session, client=client, dry_run=True, post=_post)

    assert batch.dry_run is True
    r = batch.items[0]
    assert r.status == "ok"
    assert r.pushed is False
    assert r.holdings == 1  # the fetch DID happen (operators validate it)
    # IngestionLog rolled back — no noisy fake-run entries.
    assert session.query(IngestionLog).count() == 0


# ── Scope / status filtering ─────────────────────────────────────────────────


def test_register_scope_items_are_excluded(session: Session) -> None:
    """REQ-PC-B3: only wealth-scope Items are fetched — Chase/Amex never hit
    /investments/holdings/get."""
    _make_item(session, institution_name="Chase", scope="register")
    client = _mock_client()

    batch = sync_all_wealth(session, client=client, dry_run=False)

    assert batch.items == []
    client.investments_holdings_get.assert_not_called()


def test_disconnected_and_placeholder_items_excluded(session: Session) -> None:
    _make_item(session, institution_name="Old", status="disconnected")
    _make_item(
        session, institution_name="Pending", item_id="placeholder_xyz"
    )
    client = _mock_client()

    batch = sync_all_wealth(session, client=client, dry_run=False)
    assert batch.items == []
    client.investments_holdings_get.assert_not_called()


# ── INVALID_PRODUCT skip (per-item) ──────────────────────────────────────────


def test_invalid_product_is_skip_not_failure(session: Session) -> None:
    """An Item without the investments product is skipped-with-log — clean
    exit, no OnFailure alert."""
    item = _make_item(session, institution_name="PenFed")
    client = _mock_client_raising("INVALID_PRODUCT")

    batch = sync_all_wealth(session, client=client, dry_run=False)

    r = batch.items[0]
    assert r.status == "skipped_invalid_product"
    assert r.error_code == "INVALID_PRODUCT"
    assert batch.total_failed_items == 0
    log = session.query(IngestionLog).filter_by(
        source=f"plaid_investments:{item.institution_name}"
    ).one()
    assert log.status == "success"
    assert log.error_detail is not None and "INVALID_PRODUCT" in log.error_detail


def test_invalid_product_item_does_not_block_siblings(session: Session) -> None:
    """Per-item isolation: an INVALID_PRODUCT Item never stops the next Item's
    holdings push."""
    _make_item(session, institution_name="PenFed", item_id="it_penfed")
    _make_item(session, institution_name="ETRADE", item_id="it_etrade")

    calls = {"n": 0}

    def _get(_req: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            exc = ApiException(status=400, reason="?")
            exc.body = json.dumps({"error_code": "INVALID_PRODUCT"})
            raise exc
        return SimpleNamespace(
            securities=[_security()], holdings=[_holding()], accounts=[]
        )

    client = MagicMock()
    client.investments_holdings_get.side_effect = _get
    posts: list[str] = []

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        posts.append(source)
        return {"holdings_processed": len(payload["holdings"])}

    batch = sync_all_wealth(session, client=client, dry_run=False, post=_post)

    statuses = {r.institution_name: r.status for r in batch.items}
    assert statuses == {"PenFed": "skipped_invalid_product", "ETRADE": "ok"}
    assert posts == [WEALTH_HOLDINGS_INGEST_SOURCE]
    assert batch.total_failed_items == 0


# ── Error paths ──────────────────────────────────────────────────────────────


def test_terminal_plaid_error_is_failure(session: Session) -> None:
    item = _make_item(session)
    client = _mock_client_raising("ITEM_LOGIN_REQUIRED")

    batch = sync_all_wealth(session, client=client, dry_run=False)
    r = batch.items[0]
    assert r.status == "error"
    assert r.error_code == "ITEM_LOGIN_REQUIRED"
    assert batch.total_failed_items == 1
    log = session.query(IngestionLog).filter_by(
        source=f"plaid_investments:{item.institution_name}"
    ).one()
    assert log.status == "failure"


def test_failed_d1_push_is_failure(session: Session) -> None:
    """Plaid fetch OK but the POST fails → error status (non-zero exit →
    OnFailure alert; replaces the wealth cron's silent-failure mode)."""
    from src.adapters._shared.wealth_client import WealthHTTPError

    item = _make_item(session)
    client = _mock_client()

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        raise WealthHTTPError(500, "d1 down")

    batch = sync_all_wealth(session, client=client, dry_run=False, post=_post)
    r = batch.items[0]
    assert r.status == "error"
    assert r.error_code is not None and r.error_code.startswith("D1_PUSH:")
    assert batch.total_failed_items == 1
    log = session.query(IngestionLog).filter_by(
        source=f"plaid_investments:{item.institution_name}"
    ).one()
    assert log.status == "failure"


def test_all_holdings_skipped_unmapped_is_failure_not_silent_success(
    session: Session,
) -> None:
    """P1-b2r/P1-002: A2 200s a batch where every holding's plaid_account_id
    was unmapped in D1 (e.g. a freshly re-linked Item — P0-001). `pushed`
    (rows sent) is nonzero but the endpoint's own `holdings_processed` is 0
    — this must trip `error` status (non-zero exit → OnFailure), not report
    a clean run."""
    item = _make_item(session)
    client = _mock_client()

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {
            "holdings_processed": 0,
            "holdings_skipped_unmapped": len(payload["holdings"]),
            "holdings_failed": 0,
        }

    batch = sync_all_wealth(session, client=client, dry_run=False, post=_post)
    r = batch.items[0]
    assert r.status == "error"
    assert r.holdings_written == 0
    assert r.holdings_skipped_unmapped == 1
    assert batch.total_failed_items == 1
    log = session.query(IngestionLog).filter_by(
        source=f"plaid_investments:{item.institution_name}"
    ).one()
    assert log.status == "failure"
    assert "D1_PUSH" in (log.error_detail or "")


def test_holdings_ambiguous_plaid_account_id_is_failure(session: Session) -> None:
    """P2-002: `holdings_skipped_ambiguous` (multiple D1 accounts share a
    plaid_account_id) must trip failure even when other holdings wrote."""
    _make_item(session)
    client = _mock_client()

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {
            "holdings_processed": len(payload["holdings"]),
            "holdings_skipped_ambiguous": 1,
        }

    batch = sync_all_wealth(session, client=client, dry_run=False, post=_post)
    r = batch.items[0]
    assert r.status == "error"
    assert r.holdings_skipped_ambiguous == 1


def test_holdings_partial_unmapped_is_informational_not_a_failure(
    session: Session,
) -> None:
    """Cutover policy 2026-07-26: partially-unmapped holdings mirror the
    retired wealth sync's skip-and-count behavior (E*TRADE: 3 of 8) — counted,
    logged, exit clean."""
    _make_item(session)
    client = _mock_client(holdings=[_holding(), _holding(security_id="sec_2")])

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {"holdings_processed": 1, "holdings_skipped_unmapped": 1}

    batch = sync_all_wealth(session, client=client, dry_run=False, post=_post)
    r = batch.items[0]
    assert r.status == "ok"
    assert r.holdings_skipped_unmapped == 1


def test_holdings_wholly_unmapped_item_is_a_failure(session: Session) -> None:
    """Every deliverable holding skipped-unmapped = the mapping-broke
    signature — must page."""
    _make_item(session)
    client = _mock_client(holdings=[_holding(), _holding(security_id="sec_2")])

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {"holdings_processed": 0, "holdings_skipped_unmapped": 2}

    batch = sync_all_wealth(session, client=client, dry_run=False, post=_post)
    r = batch.items[0]
    assert r.status == "error"
    assert r.error_code is not None and "D1_PUSH" in r.error_code


def test_additional_consent_required_is_a_per_item_skip(session: Session) -> None:
    """Live 2026-07-26: institutions without investment accounts answer
    ADDITIONAL_CONSENT_REQUIRED (Chase/PenFed/BofA/Citi) — same skip as
    INVALID_PRODUCT, never an error, siblings unaffected."""
    from src.adapters.plaid_client import TerminalPlaidError

    _make_item(session, institution_name="Chase", item_id="plaid_inv_chase")
    client = MagicMock()
    client.investments_holdings_get.side_effect = TerminalPlaidError(
        "ADDITIONAL_CONSENT_REQUIRED", "consent missing"
    )

    batch = sync_all_wealth(session, client=client, dry_run=False, post=MagicMock())
    r = batch.items[0]
    assert r.status == "skipped_invalid_product"
    assert r.error_code == "ADDITIONAL_CONSENT_REQUIRED"


def test_all_holdings_skipped_non_usd_is_not_a_failure(session: Session) -> None:
    """P1-r3c-2: ``holdings_skipped_non_usd`` is informational only — a batch
    the endpoint dropped entirely for currency reasons is a legitimate no-op,
    never an OnFailure alert (aligning with the A1/balance decision)."""
    item = _make_item(session)
    client = _mock_client()

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {
            "holdings_processed": 0,
            "holdings_skipped_non_usd": len(payload["holdings"]),
            "holdings_failed": 0,
        }

    batch = sync_all_wealth(session, client=client, dry_run=False, post=_post)
    r = batch.items[0]
    assert r.status == "ok"
    assert r.holdings_skipped_non_usd == 1
    assert batch.total_failed_items == 0
    log = session.query(IngestionLog).filter_by(
        source=f"plaid_investments:{item.institution_name}"
    ).one()
    assert log.status == "success"
    assert "skipped_non_usd=1" in (log.error_detail or "")


def test_endpoint_reported_holdings_failed_is_failure(session: Session) -> None:
    """The endpoint's own `holdings_failed` count must also trip failure."""
    _make_item(session)
    client = _mock_client()

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        return {"holdings_processed": 0, "holdings_failed": 1}

    batch = sync_all_wealth(session, client=client, dry_run=False, post=_post)
    r = batch.items[0]
    assert r.status == "error"
    assert r.holdings_failed_endpoint == 1
    assert batch.total_failed_items == 1


def test_undecryptable_token_is_terminal(session: Session) -> None:
    item = _make_item(session)
    item.access_token_encrypted = "REVOKED"
    session.commit()
    client = MagicMock()

    result = sync_one_item(session, item, client=client, dry_run=False)
    assert result.status == "error"
    assert result.error_code == "INVALID_ACCESS_TOKEN"
    client.investments_holdings_get.assert_not_called()


def test_item_error_does_not_block_siblings(session: Session) -> None:
    _make_item(session, institution_name="Broken", item_id="it_broken")
    _make_item(session, institution_name="Healthy", item_id="it_healthy")

    calls = {"n": 0}

    def _get(_req: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            exc = ApiException(status=400, reason="?")
            exc.body = json.dumps({"error_code": "ITEM_LOGIN_REQUIRED"})
            raise exc
        return SimpleNamespace(
            securities=[_security()], holdings=[_holding()], accounts=[]
        )

    client = MagicMock()
    client.investments_holdings_get.side_effect = _get

    batch = sync_all_wealth(
        session,
        client=client,
        dry_run=False,
        post=lambda p, s: {"holdings_processed": len(p["holdings"])},
    )
    statuses = {r.institution_name: r.status for r in batch.items}
    assert statuses == {"Broken": "error", "Healthy": "ok"}


# ── A2 chunking (contract-check fix: box must respect the 200-row cap) ───────


def test_chunk_holdings_payload_within_caps_is_passthrough() -> None:
    from src.adapters.plaid_investments import chunk_holdings_payload

    payload = {
        "item_id": "it",
        "institution_name": "Vanguard",
        "pulled_at": "2026-07-25T00:00:00+00:00",
        "fetched_at": 1,
        "securities": [{"security_id": f"s{i}"} for i in range(200)],
        "holdings": [{"security_id": f"s{i}", "account_id": "a"} for i in range(200)],
    }
    chunks = chunk_holdings_payload(payload)
    assert chunks == [payload]


def test_chunk_holdings_payload_splits_and_ships_securities_first() -> None:
    from src.adapters.plaid_investments import chunk_holdings_payload

    payload = {
        "item_id": "it",
        "institution_name": "Vanguard",
        "pulled_at": "2026-07-25T00:00:00+00:00",
        "fetched_at": 1,
        "securities": [{"security_id": f"s{i}"} for i in range(201)],
        "holdings": [{"security_id": f"s{i%201}", "account_id": "a"} for i in range(450)],
    }
    chunks = chunk_holdings_payload(payload)
    # 2 security chunks (201 -> 200+1), then 3 holding chunks (450 -> 200+200+50).
    assert [len(c["securities"]) for c in chunks] == [200, 1, 0, 0, 0]
    assert [len(c["holdings"]) for c in chunks] == [0, 0, 200, 200, 50]
    # Envelope fields carried on every chunk; every chunk within caps.
    for c in chunks:
        assert c["item_id"] == "it" and c["fetched_at"] == 1
        assert len(c["securities"]) <= 200 and len(c["holdings"]) <= 200
    # Securities all ship before the first holding.
    first_holding_idx = next(i for i, c in enumerate(chunks) if c["holdings"])
    assert all(not c["securities"] for c in chunks[first_holding_idx:])


def test_sync_one_item_posts_each_chunk(session: Session) -> None:
    item = _make_item(session)
    client = MagicMock()
    client.investments_holdings_get.return_value = SimpleNamespace(
        securities=[_security(f"sec_{i}") for i in range(201)],
        holdings=[_holding(security_id=f"sec_{i % 201}") for i in range(5)],
    )
    posts: list[tuple[dict[str, Any], str]] = []

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        posts.append((payload, source))
        return {"holdings_processed": len(payload["holdings"])}

    result = sync_one_item(
        session,
        item,
        client=client,
        dry_run=False,
        post=_post,
    )
    assert result.status == "ok" and result.pushed is True
    assert len(posts) == 3  # 200 + 1 securities chunks, then 1 holdings chunk
    assert all(src == WEALTH_HOLDINGS_INGEST_SOURCE for _, src in posts)
    assert [len(p["securities"]) for p, _ in posts] == [200, 1, 0]
    assert [len(p["holdings"]) for p, _ in posts] == [0, 0, 5]


# ── P1-xct: cross-repo golden contract fixture ──────────────────────────────
#
# tests/fixtures/plaid-consolidation-contract/*.json is a committed, literal
# slice of chunk_holdings_payload() output, loaded VERBATIM (copied, not
# symlinked — the two repos share no filesystem) by sparkry-crm-plaidcons's
# tests/unit/ingest-plaid-holdings-contract.test.ts. This test pins the
# generation side: if a future change to the chunker alters the emitted
# shape, THIS test fails here rather than the drift going unnoticed until
# both suites are (separately) green and production still breaks — the exact
# failure mode that shipped P0-a1c.

_CONTRACT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "tests"
    / "fixtures"
    / "plaid-consolidation-contract"
)


def _contract_source_item() -> Any:
    return SimpleNamespace(
        item_id="item_contract_etrade", institution_name="E*TRADE from Morgan Stanley"
    )


def _contract_security(security_id: str, *, ticker: str, name: str, sec_type: str) -> SimpleNamespace:
    """Bespoke security builder for the contract fixture — NOT the module's
    `_security()` helper (different field set/defaults; this pins the exact
    fixture-generation recipe documented in the fixture README)."""
    d = {
        "security_id": security_id,
        "isin": "US0378331005" if security_id == "sec_aapl_contract" else None,
        "cusip": "037833100" if security_id == "sec_aapl_contract" else None,
        "ticker_symbol": ticker,
        "name": name,
        "type": sec_type,
    }
    return SimpleNamespace(**d, to_dict=lambda _d=d: _d)


def _contract_holding() -> SimpleNamespace:
    d = {
        "account_id": "plaid_acct_contract_001",
        "security_id": "sec_aapl_contract",
        "quantity": 10.123456789,
        "institution_price": 233.4567891,
        "institution_value": 2363.125,
        "cost_basis": 1500.5,
        "iso_currency_code": "USD",
    }
    return SimpleNamespace(**d, to_dict=lambda _d=d: _d)


def _build_contract_chunks() -> list[dict[str, Any]]:
    """Reconstruct the exact payload the fixtures were generated from."""
    pulled_at = datetime(2026, 7, 25, 4, 20, tzinfo=UTC).replace(tzinfo=None)
    aapl = _contract_security(
        "sec_aapl_contract", ticker="AAPL", name="Apple Inc", sec_type="equity"
    )
    fillers = [
        _contract_security(
            f"sec_filler_{i}", ticker=f"FIL{i}", name=f"Filler Security {i}", sec_type="equity"
        )
        for i in range(201)
    ]
    resp = SimpleNamespace(securities=[aapl, *fillers], holdings=[_contract_holding()], accounts=[])
    payload = build_holdings_payload(_contract_source_item(), resp, pulled_at=pulled_at)
    return chunk_holdings_payload(payload)


def test_contract_fixture_matches_committed_json() -> None:
    """The committed fixtures are byte-identical to a fresh chunker run.

    If this fails, either the chunker's output shape changed (update BOTH
    this repo's fixtures AND sparkry-crm-plaidcons's copy — see the fixture
    dir's README) or the fixture-generation helper above drifted from the
    README's documented generation recipe.
    """
    chunks = _build_contract_chunks()
    securities_chunk = chunks[0]  # first securities-only chunk, carries AAPL
    holdings_chunk = chunks[-1]  # last chunk: holdings-only (securities: [])
    assert securities_chunk["securities"][0]["security_id"] == "sec_aapl_contract"
    assert holdings_chunk["securities"] == []
    assert len(holdings_chunk["holdings"]) == 1
    assert holdings_chunk["holdings"][0]["security_id"] == "sec_aapl_contract"

    committed_securities = json.loads(
        (_CONTRACT_FIXTURE_DIR / "01-securities-chunk.json").read_text()
    )
    committed_holdings = json.loads((_CONTRACT_FIXTURE_DIR / "02-holdings-chunk.json").read_text())
    assert securities_chunk == committed_securities
    assert holdings_chunk == committed_holdings
