"""Tier 3: LLM classification via Claude API.

Sends structured transaction context to Claude and parses the response into a
:class:`~src.classification.engine.ClassificationResult`. The prompt includes
entity descriptions and the full list of valid tax categories so Claude can
return a structured JSON payload.

For unit tests, inject a mock client by passing ``_client`` directly rather
than letting the function construct one from the environment.
"""

from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import anthropic

from src.classification.engine import ClassificationResult
from src.models.enums import Direction, Entity, TaxCategory
from src.models.llm_usage import LLMUsageLog, estimate_cost_for_model

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.models.transaction import Transaction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODEL = "claude-3-5-haiku-20241022"
_MAX_TOKENS = 512

# Circuit breaker settings
_CB_FAILURE_THRESHOLD = 3       # consecutive failures before opening
_CB_RECOVERY_TIMEOUT_S = 60.0   # seconds before allowing a half-open attempt

_SYSTEM_PROMPT = """\
You are an expert accountant classifying financial transactions for a \
cash-basis accounting system. You must return a single JSON object — no \
markdown, no explanation outside the JSON.

Entities:
- sparkry: Sparkry AI LLC — AI consulting and SaaS subscriptions (Schedule C)
- blackline: BlackLine MTB LLC — mountain bike parts ecommerce (Form 1065)
- personal: Personal finances of Travis Sparks (Schedule A / 1040)

Valid tax_category values (exact strings):
  Business: ADVERTISING, CAR_AND_TRUCK, CONTRACT_LABOR, INSURANCE,
            LEGAL_AND_PROFESSIONAL, OFFICE_EXPENSE, SUPPLIES,
            TAXES_AND_LICENSES, TRAVEL, MEALS, COGS,
            CONSULTING_INCOME, SUBSCRIPTION_INCOME, SALES_INCOME, REIMBURSABLE
  Personal: CHARITABLE_CASH, CHARITABLE_STOCK, MEDICAL, STATE_LOCAL_TAX,
            MORTGAGE_INTEREST, INVESTMENT_INCOME, PERSONAL_NON_DEDUCTIBLE

Valid direction values: income, expense, transfer, reimbursable

Return JSON with exactly these fields:
{
  "entity": "<entity>",
  "tax_category": "<TAX_CATEGORY>",
  "direction": "<direction>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>"
}
"""

_USER_TEMPLATE = """\
Classify this transaction:
date: {date}
amount: {amount} {currency}
description: {description}
source: {source}
subject: {subject}
from: {from_address}
body_excerpt: {body_excerpt}
"""


# ---------------------------------------------------------------------------
# Circuit breaker (module-level state, shared within the same process)
# ---------------------------------------------------------------------------


class _CircuitState:
    """Minimal mutable container for circuit breaker state."""

    def __init__(self) -> None:
        self.consecutive_failures: int = 0
        self.opened_at: float | None = None  # monotonic time when circuit opened

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def record_success(self) -> None:
        if self.is_open:
            logger.info("Circuit breaker: CLOSED (recovered after successful call)")
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= _CB_FAILURE_THRESHOLD:
            was_open = self.is_open
            self.opened_at = time.monotonic()
            if not was_open:
                logger.error(
                    "Circuit breaker: OPEN after %d consecutive failures",
                    self.consecutive_failures,
                )
            else:
                logger.warning("Circuit breaker: REOPENED after failed half-open attempt")

    def allow_attempt(self) -> bool:
        """Return True if a call should be attempted right now."""
        if not self.is_open:
            return True
        # Half-open: allow one attempt after the recovery timeout
        assert self.opened_at is not None
        elapsed = time.monotonic() - self.opened_at
        if elapsed >= _CB_RECOVERY_TIMEOUT_S:
            logger.info(
                "Circuit breaker: HALF-OPEN (%.0f s elapsed, attempting recovery)",
                elapsed,
            )
            return True
        return False


_circuit = _CircuitState()


def _reset_circuit_breaker() -> None:
    """Reset circuit breaker to closed state. Intended for use in tests only."""
    _circuit.consecutive_failures = 0
    _circuit.opened_at = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def llm_classify(
    transaction: Transaction,
    *,
    api_key: str | None = None,
    _client: anthropic.Anthropic | None = None,
    _session: Session | None = None,
) -> ClassificationResult:
    """Classify *transaction* using Claude.

    Args:
        transaction: Transaction to classify (read-only).
        api_key: Anthropic API key override. Falls back to
            ``ANTHROPIC_API_KEY`` environment variable.
        _client: Inject a pre-built (or mock) Anthropic client. When set,
            *api_key* is ignored. Intended for unit tests.
        _session: Optional SQLAlchemy session. When provided, an
            :class:`~src.models.llm_usage.LLMUsageLog` row is written after
            every successful API call so the health dashboard can show real
            cost data. The caller retains ownership of the session (commit /
            rollback / close are not managed here).

    Returns:
        A :class:`ClassificationResult` with ``tier_used=3``. On API or parse
        errors, returns a low-confidence result with the error in
        ``reasoning``. When the circuit breaker is open, returns immediately
        with ``reasoning="Circuit breaker open"`` and confidence 0.
    """
    # Circuit breaker check — skip Claude when the circuit is open
    if not _circuit.allow_attempt():
        logger.warning("Circuit breaker: OPEN — skipping Claude API call, returning needs_review")
        return _cb_fallback_result()

    client = _client or anthropic.Anthropic(
        api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    )

    raw: dict[str, Any] = (
        transaction.raw_data if isinstance(transaction.raw_data, dict) else {}
    )

    user_message = _USER_TEMPLATE.format(
        date=transaction.date,
        amount=_fmt_amount(transaction.amount),
        currency=getattr(transaction, "currency", "USD"),
        description=transaction.description,
        source=transaction.source,
        subject=raw.get("subject", ""),
        from_address=raw.get("from") or raw.get("from_address") or "",
        body_excerpt=_truncate(raw.get("body", ""), 400),
    )

    try:
        t0 = time.monotonic()
        response = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        # Successful call — reset failure counter / close circuit
        _circuit.record_success()

        # Write LLM usage log
        _write_usage_log(
            session=_session,
            response=response,
            duration_ms=duration_ms,
            transaction_id=getattr(transaction, "id", None),
        )

        first_block = response.content[0]
        # Accept TextBlock; other block types (ToolUseBlock, etc.) are unexpected.
        raw_text_val: str | None = getattr(first_block, "text", None)
        if raw_text_val is None:
            return _error_result("Unexpected response block type from Claude API")
        raw_text: str = raw_text_val.strip()
        return _parse_response(raw_text)
    except anthropic.APIError as exc:
        logger.error("Anthropic API error: %s", exc)
        _circuit.record_failure()
        return _error_result(f"Anthropic API error: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in LLM classifier: %s", exc)
        _circuit.record_failure()
        return _error_result(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_usage_log(
    *,
    session: Session | None,
    response: Any,
    duration_ms: int,
    transaction_id: str | None,
) -> None:
    """Persist an :class:`LLMUsageLog` row for *response*.

    Silently skips if *session* is ``None``. Any DB error is logged but not
    re-raised — usage logging must never interrupt the classification pipeline.
    """
    if session is None:
        return
    try:
        _raw_model = getattr(response, "model", _MODEL)
        model_name: str = str(_raw_model) if isinstance(_raw_model, str) else _MODEL
        usage = getattr(response, "usage", None)
        input_tokens: int = int(getattr(usage, "input_tokens", 0))
        output_tokens: int = int(getattr(usage, "output_tokens", 0))
        cost = estimate_cost_for_model(model_name, input_tokens, output_tokens)

        log_entry = LLMUsageLog(
            model=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
            duration_ms=duration_ms,
            transaction_id=transaction_id,
        )
        session.add(log_entry)
        session.flush()
        logger.debug(
            "LLMUsageLog written: model=%s in=%d out=%d cost=$%.6f duration=%dms",
            model_name,
            input_tokens,
            output_tokens,
            cost,
            duration_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to write LLMUsageLog: %s", exc)


def _parse_response(raw_text: str) -> ClassificationResult:
    """Parse Claude's JSON response into a ClassificationResult.

    On any parse / validation error, returns a low-confidence fallback result
    rather than raising — the engine will route it to needs_review.
    """
    # Strip optional markdown code fences.
    text = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError as exc:
        return _error_result(f"JSON parse error: {exc}. Raw: {raw_text[:200]}")

    try:
        entity = Entity(data["entity"])
    except (KeyError, ValueError):
        return _error_result(f"Invalid entity: {data.get('entity')!r}")

    try:
        tax_category = TaxCategory(data["tax_category"])
    except (KeyError, ValueError):
        return _error_result(f"Invalid tax_category: {data.get('tax_category')!r}")

    try:
        direction = Direction(data["direction"])
    except (KeyError, ValueError):
        return _error_result(f"Invalid direction: {data.get('direction')!r}")

    try:
        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.0

    reasoning: str = str(data.get("reasoning", "No reasoning provided."))

    return ClassificationResult(
        entity=entity,
        tax_category=tax_category,
        direction=direction,
        confidence=confidence,
        tier_used=3,
        reasoning=reasoning,
    )


def _error_result(reason: str) -> ClassificationResult:
    """Return a low-confidence placeholder result for error cases."""
    return ClassificationResult(
        entity=Entity.PERSONAL,
        tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE,
        direction=Direction.EXPENSE,
        confidence=0.0,
        tier_used=3,
        reasoning=reason,
    )


def _cb_fallback_result() -> ClassificationResult:
    """Return the standard circuit-breaker open fallback result."""
    return ClassificationResult(
        entity=Entity.PERSONAL,
        tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE,
        direction=Direction.EXPENSE,
        confidence=0.0,
        tier_used=3,
        reasoning="Circuit breaker open",
    )


def _fmt_amount(amount: Any) -> str:
    if isinstance(amount, Decimal):
        return f"{amount:,.2f}"
    try:
        return f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        return str(amount)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"
