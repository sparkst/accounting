"""§7 (revised): /api/ingest/run accepts either key; brokerage-csv + reclassify are browser-key only."""
from fastapi.testclient import TestClient


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
