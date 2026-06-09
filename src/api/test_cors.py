"""REQ-HM-017: the production origin must be in CORS allow_origins."""
from fastapi.testclient import TestClient

from src.api.main import app


def test_books_origin_allowed():
    client = TestClient(app)
    resp = client.options(
        "/api/health/ping",
        headers={
            "Origin": "https://books.sparkry.ai",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "https://books.sparkry.ai"
