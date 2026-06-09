"""§7: production boot assertion for API_KEY / INGEST_API_KEY."""
import pytest

from src.api._startup_assert import assert_production_secrets

STRONG_A = "a" * 32
STRONG_B = "b" * 32


def _env(monkeypatch, **kw):
    for k in ("PLAID_ENV", "API_KEY", "INGEST_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_missing_api_key_in_production_raises(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", INGEST_API_KEY=STRONG_B)
    with pytest.raises(RuntimeError, match="API_KEY"):
        assert_production_secrets()


def test_missing_ingest_key_in_production_raises(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", API_KEY=STRONG_A)
    with pytest.raises(RuntimeError, match="INGEST_API_KEY"):
        assert_production_secrets()


def test_weak_key_raises(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", API_KEY="short", INGEST_API_KEY=STRONG_B)
    with pytest.raises(RuntimeError, match="32"):
        assert_production_secrets()


def test_equal_keys_raise(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", API_KEY=STRONG_A, INGEST_API_KEY=STRONG_A)
    with pytest.raises(RuntimeError, match="must differ"):
        assert_production_secrets()


def test_two_distinct_strong_keys_ok(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", API_KEY=STRONG_A, INGEST_API_KEY=STRONG_B)
    assert_production_secrets()  # no raise


def test_non_production_is_permissive(monkeypatch):
    _env(monkeypatch)  # PLAID_ENV unset → not production
    assert_production_secrets()  # no raise
