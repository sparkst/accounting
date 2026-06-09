"""§7 (revised): /api/ingest/run accepts either key; brokerage-csv + reclassify are browser-key only.

Auth-ONLY tests: the ingest + reclassify WORK is stubbed so an auth-passing POST
does NOT run the real adapters / classification engine. Running it for real makes
failing Claude API calls that trip the module-level circuit breaker in
src.classification.llm_classifier — global state that then pollutes later
classifier tests (they'd see "Circuit breaker open" instead of "API error").
These tests assert ONLY the auth outcome (401 vs not).
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _stub_ingest_work(monkeypatch):
    """Stub the ingest + reclassify work so auth-passing POSTs do no real work
    (no adapters, no classifier, no Claude calls → no circuit-breaker pollution)."""
    import src.api.routes.ingest as ingest_mod

    monkeypatch.setattr(
        ingest_mod,
        "_run_ingest_locked",
        lambda source=None: ingest_mod.IngestSummary(
            ingested_count=0,
            classified_count=0,
            needs_review_count=0,
            adapter_results=[],
            warnings=[],
            errors=[],
        ),
    )
    monkeypatch.setattr(
        ingest_mod,
        "reclassify_all",
        lambda *a, **k: SimpleNamespace(
            vendor_updated=0, classified=0, still_needs_review=0, errors=[]
        ),
    )


def _client(monkeypatch):
    monkeypatch.setenv("API_KEY", "k" * 32)
    monkeypatch.setenv("INGEST_API_KEY", "i" * 32)
    from src.api.main import app
    return TestClient(app)


def test_ingest_run_accepts_browser_key(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/ingest/run", headers={"X-Api-Key": "k" * 32})
    assert r.status_code != 401  # dashboard 'Sync Now' must work


def test_ingest_run_accepts_ingest_key(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/ingest/run", headers={"X-Api-Key": "i" * 32})
    assert r.status_code != 401  # n8n must work


def test_ingest_run_rejects_unknown_key(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/ingest/run", headers={"X-Api-Key": "z" * 32})
    assert r.status_code == 401


def test_brokerage_csv_accepts_browser_key_rejects_ingest_key(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/import/brokerage-csv", headers={"X-Api-Key": "k" * 32}).status_code != 401
    assert c.post("/api/import/brokerage-csv", headers={"X-Api-Key": "i" * 32}).status_code == 401


def test_reclassify_accepts_browser_key_rejects_ingest_key(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/ingest/reclassify", headers={"X-Api-Key": "k" * 32}).status_code != 401
    assert c.post("/api/ingest/reclassify", headers={"X-Api-Key": "i" * 32}).status_code == 401
