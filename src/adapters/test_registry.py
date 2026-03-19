"""Tests for the adapter registry.

REQ-ID: ADAPTER-REG-001  Lazy factory maps Source enum values to adapter classes.
REQ-ID: ADAPTER-REG-002  Missing API keys return None with a warning, never raise.
REQ-ID: ADAPTER-REG-003  Adapters are constructed only when get_adapter() is called.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient  # noqa: E402 — third-party after stdlib

from src.adapters import INGEST_SOURCES, get_adapter
from src.models.enums import Source

# ---------------------------------------------------------------------------
# get_adapter — happy paths
# ---------------------------------------------------------------------------


def test_get_adapter_gmail_returns_adapter() -> None:
    """GmailN8nAdapter requires no env vars — should always succeed."""
    adapter = get_adapter(Source.GMAIL_N8N)
    assert adapter is not None
    assert adapter.source == Source.GMAIL_N8N.value


def test_get_adapter_deduction_email_returns_adapter() -> None:
    """DeductionEmailAdapter requires no env vars — should always succeed."""
    adapter = get_adapter(Source.DEDUCTION_EMAIL)
    assert adapter is not None
    assert adapter.source == Source.DEDUCTION_EMAIL.value


def test_get_adapter_stripe_returns_adapter_when_keys_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """StripeAdapter is returned when both Stripe API keys are set."""
    monkeypatch.setenv("STRIPE_API_KEY_SPARKRY", "sk_test_sparkry")
    monkeypatch.setenv("STRIPE_API_KEY_BLACKLINE", "sk_test_blackline")

    adapter = get_adapter(Source.STRIPE)
    assert adapter is not None
    assert adapter.source == Source.STRIPE.value


def test_get_adapter_shopify_returns_adapter_when_keys_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """ShopifyAdapter is returned when both Shopify env vars are set."""
    monkeypatch.setenv("SHOPIFY_API_KEY", "shpat_test")
    monkeypatch.setenv("SHOPIFY_STORE_URL", "test.myshopify.com")

    adapter = get_adapter(Source.SHOPIFY)
    assert adapter is not None
    assert adapter.source == Source.SHOPIFY.value


# ---------------------------------------------------------------------------
# get_adapter — missing keys → None, no exception
# ---------------------------------------------------------------------------


def test_get_adapter_stripe_missing_sparkry_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing STRIPE_API_KEY_SPARKRY → None (no exception)."""
    monkeypatch.delenv("STRIPE_API_KEY_SPARKRY", raising=False)
    monkeypatch.setenv("STRIPE_API_KEY_BLACKLINE", "sk_test_blackline")

    adapter = get_adapter(Source.STRIPE)
    assert adapter is None


def test_get_adapter_stripe_missing_both_keys_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both Stripe keys missing → None."""
    monkeypatch.delenv("STRIPE_API_KEY_SPARKRY", raising=False)
    monkeypatch.delenv("STRIPE_API_KEY_BLACKLINE", raising=False)

    adapter = get_adapter(Source.STRIPE)
    assert adapter is None


def test_get_adapter_shopify_missing_api_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing SHOPIFY_API_KEY → None (no exception)."""
    monkeypatch.delenv("SHOPIFY_API_KEY", raising=False)
    monkeypatch.setenv("SHOPIFY_STORE_URL", "test.myshopify.com")

    adapter = get_adapter(Source.SHOPIFY)
    assert adapter is None


def test_get_adapter_shopify_missing_store_url_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing SHOPIFY_STORE_URL → None."""
    monkeypatch.setenv("SHOPIFY_API_KEY", "shpat_test")
    monkeypatch.delenv("SHOPIFY_STORE_URL", raising=False)

    adapter = get_adapter(Source.SHOPIFY)
    assert adapter is None


def test_get_adapter_missing_keys_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A WARNING is emitted (not an ERROR or exception) for missing keys."""
    import logging

    monkeypatch.delenv("STRIPE_API_KEY_SPARKRY", raising=False)
    monkeypatch.delenv("STRIPE_API_KEY_BLACKLINE", raising=False)

    with caplog.at_level(logging.WARNING, logger="src.adapters"):
        get_adapter(Source.STRIPE)

    warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("stripe" in m.lower() for m in warning_messages)


# ---------------------------------------------------------------------------
# get_adapter — unregistered upload-only sources
# ---------------------------------------------------------------------------


def test_get_adapter_brokerage_csv_returns_none() -> None:
    """BROKERAGE_CSV is upload-only — not part of the automated ingest loop."""
    adapter = get_adapter(Source.BROKERAGE_CSV)
    assert adapter is None


def test_get_adapter_bank_csv_returns_none() -> None:
    """BANK_CSV is upload-only — not part of the automated ingest loop."""
    adapter = get_adapter(Source.BANK_CSV)
    assert adapter is None


def test_get_adapter_photo_receipt_returns_none() -> None:
    """PHOTO_RECEIPT is not yet implemented."""
    adapter = get_adapter(Source.PHOTO_RECEIPT)
    assert adapter is None


# ---------------------------------------------------------------------------
# INGEST_SOURCES list
# ---------------------------------------------------------------------------


def test_ingest_sources_contains_automated_adapters() -> None:
    """INGEST_SOURCES includes the four automated-ingest adapters."""
    assert Source.GMAIL_N8N in INGEST_SOURCES
    assert Source.DEDUCTION_EMAIL in INGEST_SOURCES
    assert Source.STRIPE in INGEST_SOURCES
    assert Source.SHOPIFY in INGEST_SOURCES


def test_ingest_sources_excludes_upload_only() -> None:
    """Upload-only sources must not appear in INGEST_SOURCES."""
    assert Source.BROKERAGE_CSV not in INGEST_SOURCES
    assert Source.BANK_CSV not in INGEST_SOURCES


# ---------------------------------------------------------------------------
# Concurrency guard — HTTP 409 when ingest is already running
# ---------------------------------------------------------------------------


def test_ingest_run_returns_409_when_already_running() -> None:
    """POST /api/ingest/run returns 409 Conflict if a run is already in progress."""
    from src.api.routes import ingest as ingest_module  # noqa: PLC0415

    # Temporarily grab the lock to simulate an in-progress run.
    ingest_module._ingest_lock.acquire()
    try:
        from src.api.main import app  # noqa: PLC0415

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/api/ingest/run")
        assert response.status_code == 409
        assert "already in progress" in response.json()["detail"].lower()
    finally:
        ingest_module._ingest_lock.release()


# ---------------------------------------------------------------------------
# Source query parameter — single-source run
# ---------------------------------------------------------------------------


def test_ingest_run_with_source_param_runs_only_that_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """?source=gmail_n8n triggers only the Gmail adapter, not all adapters."""
    from src.api.main import app  # noqa: PLC0415

    run_calls: list[str] = []

    class _FakeGmailAdapter:
        source = "gmail_n8n"

        def run(self, session: object) -> object:
            run_calls.append("gmail_n8n")
            result = MagicMock()
            result.records_created = 0
            result.records_processed = 0
            result.records_skipped = 0
            result.records_failed = 0
            result.errors = []
            result.status.value = "success"
            return result

    class _FakeStripeAdapter:
        source = "stripe"

        def run(self, session: object) -> object:
            run_calls.append("stripe")
            result = MagicMock()
            result.records_created = 0
            result.records_processed = 0
            result.records_skipped = 0
            result.records_failed = 0
            result.errors = []
            result.status.value = "success"
            return result

    def _fake_get_adapter(src: Source) -> object | None:
        if src == Source.GMAIL_N8N:
            return _FakeGmailAdapter()
        if src == Source.STRIPE:
            return _FakeStripeAdapter()
        return None

    with (
        patch("src.api.routes.ingest.get_adapter", side_effect=_fake_get_adapter),
        patch("src.api.routes.ingest.SessionLocal"),
        patch("src.api.routes.ingest.classify", side_effect=Exception("no DB")),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/api/ingest/run?source=gmail_n8n")

    # Only gmail_n8n should have been called
    assert run_calls == ["gmail_n8n"]


def test_ingest_run_without_source_param_runs_all_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting ?source runs all INGEST_SOURCES adapters."""
    from src.api.main import app  # noqa: PLC0415

    run_calls: list[str] = []

    def _fake_get_adapter(src: Source) -> object | None:
        class _FakeAdapter:
            def __init__(self, name: str) -> None:
                self.source = name

            def run(self, session: object) -> object:
                run_calls.append(self.source)
                result = MagicMock()
                result.records_created = 0
                result.records_processed = 0
                result.records_skipped = 0
                result.records_failed = 0
                result.errors = []
                result.status.value = "success"
                return result

        # Return a fake adapter for all INGEST_SOURCES
        if src in INGEST_SOURCES:
            return _FakeAdapter(src.value)
        return None

    with (
        patch("src.api.routes.ingest.get_adapter", side_effect=_fake_get_adapter),
        patch("src.api.routes.ingest.SessionLocal"),
        patch("src.api.routes.ingest.classify", side_effect=Exception("no DB")),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/api/ingest/run")

    # All INGEST_SOURCES should have been called
    for src in INGEST_SOURCES:
        assert src.value in run_calls


def test_ingest_run_skips_adapter_with_missing_keys_and_includes_warning() -> None:
    """When get_adapter returns None, source is skipped and a warning is included."""
    from src.api.main import app  # noqa: PLC0415

    def _fake_get_adapter(src: Source) -> object | None:
        # Simulate all adapters unavailable
        return None

    with (
        patch("src.api.routes.ingest.get_adapter", side_effect=_fake_get_adapter),
        patch("src.api.routes.ingest.SessionLocal"),
        patch("src.api.routes.ingest.classify", side_effect=Exception("no DB")),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/api/ingest/run")

    assert response.status_code == 200
    body = response.json()
    assert len(body["warnings"]) > 0
    # Each warning should mention the source being unavailable
    for w in body["warnings"]:
        assert "unavailable" in w.lower() or "api key" in w.lower() or "missing" in w.lower()


def test_ingest_run_invalid_source_returns_422() -> None:
    """An unrecognised ?source value returns HTTP 422 (FastAPI enum validation)."""
    from src.api.main import app  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/ingest/run?source=nonexistent_source")
    assert response.status_code == 422
