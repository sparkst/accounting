"""§7: /api/health/ping is public+minimal; rich health + source-config require auth."""
from fastapi.testclient import TestClient


def _client(monkeypatch):
    monkeypatch.setenv("API_KEY", "k" * 32)
    monkeypatch.setenv("INGEST_API_KEY", "i" * 32)
    from src.api.main import app
    return TestClient(app)


def test_ping_is_public_and_minimal(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/health/ping")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True}
    for leak in ("error_detail", "institution_name", "llm_usage", "failure_log"):
        assert leak not in body


def test_rich_health_requires_auth(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/health").status_code == 401
    assert c.get("/api/health", headers={"X-Api-Key": "k" * 32}).status_code == 200


def test_source_config_requires_auth_and_scrubs_dotenv(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/health/source-config").status_code == 401
    r = c.get("/api/health/source-config", headers={"X-Api-Key": "k" * 32})
    assert r.status_code == 200
    assert ".env" not in r.text  # stale disclosure scrubbed
