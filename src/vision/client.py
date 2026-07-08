"""Vision provider abstraction (REQ-VIS-001).

Two providers behind a :class:`VisionProvider` protocol:

* :class:`GeminiVisionProvider` — default, ``gemini-2.5-flash``, native PDF input.
* :class:`OpenAIVisionProvider` — fallback, ``gpt-4o-mini``. The ``openai``
  package is imported LAZILY (``importlib``) inside the call path so this module
  imports cleanly even when ``openai`` is not installed.

``select_provider`` reads the ``VISION_PROVIDER`` env var (default ``gemini``).
Each provider carries its own circuit breaker (a clone of
``src.classification.llm_classifier._CircuitState``) and logs one
``LLMUsageLog`` row per call via ``estimate_cost_for_model``. API keys come only
from the environment (``GEMINI_API_KEY`` / ``OPENAI_API_KEY``) and are never
logged. For tests, inject a fake client via the ``_client`` constructor param —
no network and no real SDK import occur.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from google import genai
from google.genai import types as genai_types

from src.models.llm_usage import LLMUsageLog, estimate_cost_for_model

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Models ───────────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-2.5-flash"
OPENAI_MODEL = "gpt-4o-mini"

# Circuit breaker settings (mirrors llm_classifier).
_CB_FAILURE_THRESHOLD = 3
_CB_RECOVERY_TIMEOUT_S = 60.0


# ── Errors ───────────────────────────────────────────────────────────────────


class VisionError(Exception):
    """Base error for vision extraction failures (isolated per-file upstream)."""


class VisionCircuitOpen(VisionError):
    """Raised when a provider's circuit breaker is open — no call is attempted."""


# ── Extraction result ────────────────────────────────────────────────────────


@dataclass
class VisionExtraction:
    """The normalized result of a single provider ``extract`` call."""

    fields: dict[str, Any]
    raw_response: dict[str, Any] | str
    model: str
    input_tokens: int
    output_tokens: int
    cost_estimate: float
    duration_ms: int


# ── Circuit breaker (per-provider clone of llm_classifier._CircuitState) ──────


class _CircuitState:
    """Minimal mutable container for per-provider circuit breaker state."""

    def __init__(self) -> None:
        self.consecutive_failures: int = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def record_success(self) -> None:
        if self.is_open:
            logger.info("Vision circuit breaker: CLOSED (recovered)")
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= _CB_FAILURE_THRESHOLD:
            was_open = self.is_open
            self.opened_at = time.monotonic()
            if not was_open:
                logger.error(
                    "Vision circuit breaker: OPEN after %d consecutive failures",
                    self.consecutive_failures,
                )

    def allow_attempt(self) -> bool:
        if not self.is_open:
            return True
        assert self.opened_at is not None
        if time.monotonic() - self.opened_at >= _CB_RECOVERY_TIMEOUT_S:
            logger.info("Vision circuit breaker: HALF-OPEN (attempting recovery)")
            return True
        return False


# ── Provider protocol ────────────────────────────────────────────────────────


@runtime_checkable
class VisionProvider(Protocol):
    """Structural type every vision provider satisfies."""

    name: str

    def extract(
        self,
        file_bytes: bytes,
        mime: str,
        schema: dict[str, Any],
        prompt: str,
        *,
        session: Session | None = None,
    ) -> VisionExtraction: ...


# ── Usage logging ────────────────────────────────────────────────────────────


def _write_usage_log(
    session: Session | None,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_estimate: float,
    duration_ms: int,
) -> None:
    """Persist one ``LLMUsageLog`` row for a vision call (no-op without session).

    Any DB error is logged and swallowed — usage logging never breaks a run.
    """
    if session is None:
        return
    try:
        session.add(
            LLMUsageLog(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_estimate=cost_estimate,
                duration_ms=duration_ms,
                transaction_id=None,
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 — logging must not interrupt the run
        logger.error("Failed to write vision LLMUsageLog: %s", exc)


def _loads(text: str) -> dict[str, Any]:
    """Parse a JSON object from provider text, tolerating markdown fences."""
    cleaned = (
        text.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise VisionError("provider returned non-object JSON")
    return data


# ── Gemini provider ──────────────────────────────────────────────────────────


@dataclass
class GeminiVisionProvider:
    """Default provider — Gemini ``gemini-2.5-flash`` with native PDF input."""

    name: str = "gemini"
    model: str = GEMINI_MODEL
    api_key: str | None = None
    _client: Any | None = None
    _circuit: _CircuitState = field(default_factory=_CircuitState)

    def extract(
        self,
        file_bytes: bytes,
        mime: str,
        schema: dict[str, Any],
        prompt: str,
        *,
        session: Session | None = None,
    ) -> VisionExtraction:
        if not self._circuit.allow_attempt():
            raise VisionCircuitOpen("gemini circuit breaker open")

        client = self._client or genai.Client(
            api_key=self.api_key or os.environ.get("GEMINI_API_KEY", "")
        )
        try:
            t0 = time.monotonic()
            contents: list[Any] = [
                genai_types.Part.from_bytes(data=file_bytes, mime_type=mime),
                prompt,
            ]
            response = client.models.generate_content(
                model=self.model,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction="You extract structured financial "
                    "statement fields as strict JSON matching the provided "
                    "schema. Return money as strings with two decimals.",
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001 — one retry budget per provider
            self._circuit.record_failure()
            raise VisionError(f"gemini extraction failed: {exc}") from exc

        self._circuit.record_success()

        raw_text = response.text
        if raw_text is None:
            raise VisionError("gemini returned an empty response")
        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        model_version = str(getattr(response, "model_version", None) or self.model)
        cost = estimate_cost_for_model(model_version, input_tokens, output_tokens)

        _write_usage_log(
            session,
            model=model_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
            duration_ms=duration_ms,
        )
        return VisionExtraction(
            fields=_loads(raw_text),
            raw_response=raw_text,
            model=model_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
            duration_ms=duration_ms,
        )


# ── OpenAI provider (lazy import) ────────────────────────────────────────────


@dataclass
class OpenAIVisionProvider:
    """Fallback provider — OpenAI ``gpt-4o-mini``.

    The ``openai`` package is imported lazily inside :meth:`extract` (only when
    no ``_client`` is injected) so this module imports and type-checks without
    the dependency installed.
    """

    name: str = "openai"
    model: str = OPENAI_MODEL
    api_key: str | None = None
    _client: Any | None = None
    _circuit: _CircuitState = field(default_factory=_CircuitState)

    def _make_client(self) -> Any:
        if self._client is not None:
            return self._client
        openai_mod = importlib.import_module("openai")
        return openai_mod.OpenAI(api_key=self.api_key or os.environ.get("OPENAI_API_KEY", ""))

    def extract(
        self,
        file_bytes: bytes,
        mime: str,
        schema: dict[str, Any],
        prompt: str,
        *,
        session: Session | None = None,
    ) -> VisionExtraction:
        if not self._circuit.allow_attempt():
            raise VisionCircuitOpen("openai circuit breaker open")

        client = self._make_client()
        try:
            t0 = time.monotonic()
            response = client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": "You extract structured financial statement "
                        "fields as strict JSON. Return money as strings with two "
                        "decimals.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001 — one retry budget per provider
            self._circuit.record_failure()
            raise VisionError(f"openai extraction failed: {exc}") from exc

        self._circuit.record_success()

        raw_text = response.choices[0].message.content
        if raw_text is None:
            raise VisionError("openai returned an empty response")
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        model_version = str(getattr(response, "model", None) or self.model)
        cost = estimate_cost_for_model(model_version, input_tokens, output_tokens)

        _write_usage_log(
            session,
            model=model_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
            duration_ms=duration_ms,
        )
        return VisionExtraction(
            fields=_loads(raw_text),
            raw_response=raw_text,
            model=model_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
            duration_ms=duration_ms,
        )


# ── Selection ────────────────────────────────────────────────────────────────


def select_provider(
    name: str | None = None, *, _client: Any | None = None
) -> VisionProvider:
    """Return a provider by name, defaulting to the ``VISION_PROVIDER`` env var.

    ``name`` (or env) is one of ``gemini`` | ``openai``. ``_client`` injects a
    fake client for tests. Raises ``ValueError`` on an unknown provider name.
    """
    resolved = (name or os.environ.get("VISION_PROVIDER") or "gemini").strip().lower()
    if resolved == "gemini":
        return GeminiVisionProvider(_client=_client)
    if resolved == "openai":
        return OpenAIVisionProvider(_client=_client)
    raise ValueError(f"unknown vision provider: {resolved!r}")


__all__ = [
    "GEMINI_MODEL",
    "OPENAI_MODEL",
    "GeminiVisionProvider",
    "OpenAIVisionProvider",
    "VisionCircuitOpen",
    "VisionError",
    "VisionExtraction",
    "VisionProvider",
    "select_provider",
]
