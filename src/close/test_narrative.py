"""Tests for the optional Gemini narrative (REQ-MCA-004, spec §1.5)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from src.close.anomalies import AnomalyReport
from src.close.narrative import _circuit, _reset_circuit_breaker, build_prompt, render_narrative
from src.close.reconcile import ReconcileSummary
from src.close.report import AutoConfirmSummary, CloseReport


def _report() -> CloseReport:
    return CloseReport(
        month="2026-06",
        generated_at="2026-07-07T15:00:00+00:00",
        rows_ingested=42,
        needs_review_depth=4,
        reconcile=ReconcileSummary(month="2026-06"),
        anomalies=AnomalyReport(month="2026-06"),
        autoconfirm=AutoConfirmSummary(total=12),
    )


class _FakeResponse:
    text = "Sentence one. Two. Three. Four. Five."
    usage_metadata = type("U", (), {"prompt_token_count": 30, "candidates_token_count": 40})()
    model_version = "gemini-2.5-flash-lite"


class _FakeClient:
    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

        class _Models:
            def generate_content(inner, **kwargs: Any) -> _FakeResponse:  # noqa: N805
                self.captured.update(kwargs)
                return _FakeResponse()

        self.models = _Models()


@pytest.fixture(autouse=True)
def _reset() -> None:
    _reset_circuit_breaker()


def test_flag_off_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-MCA-004: with CLOSE_NARRATIVE_LLM unset the narrative is skipped."""
    monkeypatch.delenv("CLOSE_NARRATIVE_LLM", raising=False)
    assert render_narrative(_report(), _client=_FakeClient()) is None  # type: ignore[arg-type]


def test_breaker_open_returns_none_report_still_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-MCA-004: an open breaker yields None (report still sends) with no call."""
    monkeypatch.setenv("CLOSE_NARRATIVE_LLM", "1")
    _circuit.consecutive_failures = 3
    _circuit.opened_at = time.monotonic()
    client = _FakeClient()
    assert render_narrative(_report(), _client=client) is None  # type: ignore[arg-type]
    assert client.captured == {}  # breaker short-circuits before any provider call


def test_enabled_returns_text_and_prompt_is_aggregates_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-MCA-004: enabled path returns prose; the prompt carries aggregates only."""
    monkeypatch.setenv("CLOSE_NARRATIVE_LLM", "1")
    client = _FakeClient()
    out = render_narrative(_report(), _client=client)  # type: ignore[arg-type]
    assert out == "Sentence one. Two. Three. Four. Five."
    prompt = client.captured["contents"]
    assert prompt == build_prompt(_report())
    assert "rows_ingested: 42" in prompt
    assert "auto_confirmed: 12" in prompt
    # Aggregates only — no raw transaction descriptors leak into the prompt.
    assert "raw_data" not in prompt and "description" not in prompt


def test_provider_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-MCA-004: a provider exception is swallowed to None (additive only)."""
    monkeypatch.setenv("CLOSE_NARRATIVE_LLM", "1")

    class _Boom:
        def __init__(self) -> None:
            class _Models:
                def generate_content(self, **kwargs: Any) -> Any:
                    raise RuntimeError("gemini down")

            self.models = _Models()

    assert render_narrative(_report(), _client=_Boom()) is None  # type: ignore[arg-type]
