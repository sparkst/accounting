"""Tests for vision provider abstraction (Gemini + OpenAI, circuit breaker, usage log).

REQ-VIS-001: provider-configurable vision extraction, usage logged, keys from env.
REQ-VIS-004: every call logs an llm_usage_log row with cost.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.db.connection import _configure_sqlite
from src.models import transaction as _transaction  # noqa: F401 — register tables
from src.models.base import Base
from src.models.llm_usage import LLMUsageLog
from src.vision import client as client_mod
from src.vision.client import (
    GeminiVisionProvider,
    OpenAIVisionProvider,
    VisionCircuitOpen,
    VisionError,
    select_provider,
)

_FIELDS = {
    "institution": "F&G",
    "account_number_mask": "****2585",
    "as_of": "2026-05-07",
    "balance": "660218.55",
}


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    event.listen(eng, "connect", _configure_sqlite)
    Base.metadata.create_all(bind=eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _make_gemini_client(
    fields: dict[str, Any] | None = None,
    *,
    input_tokens: int = 4200,
    output_tokens: int = 900,
    model: str = "gemini-2.5-flash",
) -> MagicMock:
    resp = MagicMock()
    resp.text = json.dumps(fields or _FIELDS)
    resp.usage_metadata.prompt_token_count = input_tokens
    resp.usage_metadata.candidates_token_count = output_tokens
    resp.model_version = model
    client = MagicMock()
    client.models.generate_content.return_value = resp
    return client


def _make_failing_gemini_client() -> MagicMock:
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("api down")
    return client


def test_gemini_extract_returns_fields_and_cost() -> None:
    """REQ-VIS-001: Gemini provider parses JSON fields and computes cost."""
    provider = GeminiVisionProvider(_client=_make_gemini_client())
    result = provider.extract(b"%PDF-1.4", "application/pdf", {}, "prompt")
    assert result.fields == _FIELDS
    assert result.model == "gemini-2.5-flash"
    assert result.input_tokens == 4200
    assert result.cost_estimate > 0


def test_gemini_extract_writes_usage_log(session: Session) -> None:
    """REQ-VIS-004: a successful extract writes one llm_usage_log row."""
    provider = GeminiVisionProvider(_client=_make_gemini_client())
    provider.extract(b"%PDF-1.4", "application/pdf", {}, "prompt", session=session)
    rows = session.query(LLMUsageLog).all()
    assert len(rows) == 1
    assert rows[0].model == "gemini-2.5-flash"
    assert rows[0].cost_estimate > 0
    assert rows[0].transaction_id is None


def test_gemini_circuit_breaker_opens_after_three_failures() -> None:
    """REQ-VIS-001: 3 consecutive failures open the breaker; the 4th call short-circuits."""
    failing = _make_failing_gemini_client()
    provider = GeminiVisionProvider(_client=failing)
    for _ in range(3):
        with pytest.raises(VisionError):
            provider.extract(b"x", "application/pdf", {}, "p")
    with pytest.raises(VisionCircuitOpen):
        provider.extract(b"x", "application/pdf", {}, "p")
    # The 4th attempt never reached the client.
    assert failing.models.generate_content.call_count == 3


def test_gemini_empty_response_raises() -> None:
    """REQ-VIS-001: an empty provider response is a VisionError."""
    client = MagicMock()
    resp = MagicMock()
    resp.text = None
    client.models.generate_content.return_value = resp
    provider = GeminiVisionProvider(_client=client)
    with pytest.raises(VisionError):
        provider.extract(b"x", "application/pdf", {}, "p")


def _make_openai_client(fields: dict[str, Any] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.choices[0].message.content = json.dumps(fields or _FIELDS)
    resp.usage.prompt_tokens = 3000
    resp.usage.completion_tokens = 500
    resp.model = "gpt-4o-mini"
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


def test_openai_provider_extract_with_injected_client(session: Session) -> None:
    """REQ-VIS-001: OpenAI fallback parses fields + logs usage (no network, no import)."""
    provider = OpenAIVisionProvider(_client=_make_openai_client())
    result = provider.extract(b"x", "application/pdf", {}, "p", session=session)
    assert result.fields == _FIELDS
    assert result.model == "gpt-4o-mini"
    assert result.cost_estimate > 0
    assert session.query(LLMUsageLog).count() == 1


def test_select_provider_default_and_named() -> None:
    """REQ-VIS-001: select_provider honors name arg; defaults to gemini."""
    assert isinstance(select_provider("gemini"), GeminiVisionProvider)
    assert isinstance(select_provider("openai"), OpenAIVisionProvider)
    assert isinstance(select_provider(None), GeminiVisionProvider)


def test_select_provider_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-VIS-001: VISION_PROVIDER env selects the provider."""
    monkeypatch.setenv("VISION_PROVIDER", "openai")
    assert isinstance(select_provider(), OpenAIVisionProvider)


def test_select_provider_unknown_raises() -> None:
    """REQ-VIS-001: an unknown provider name is rejected."""
    with pytest.raises(ValueError):
        select_provider("anthropic")


def test_gpt_4o_mini_pricing_present() -> None:
    """REQ-VIS-001: gpt-4o-mini pricing prefix added to _PRICING."""
    from src.models.llm_usage import _PRICING, estimate_cost_for_model

    assert "gpt-4o-mini" in _PRICING
    # 1M input @ $0.15, 1M output @ $0.60
    assert abs(estimate_cost_for_model("gpt-4o-mini", 1_000_000, 0) - 0.15) < 1e-9
    assert abs(estimate_cost_for_model("gpt-4o-mini", 0, 1_000_000) - 0.60) < 1e-9


def test_module_imports_without_openai_installed() -> None:
    """REQ-VIS-001: client module imports even though openai is not installed."""
    # If the import were top-level this test module would have failed to import.
    assert client_mod.OPENAI_MODEL == "gpt-4o-mini"
