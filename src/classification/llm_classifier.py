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
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import anthropic

from src.classification.engine import ClassificationResult
from src.models.enums import Direction, Entity, TaxCategory

if TYPE_CHECKING:
    from src.models.transaction import Transaction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODEL = "claude-3-5-haiku-20241022"
_MAX_TOKENS = 512

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
# Public API
# ---------------------------------------------------------------------------


def llm_classify(
    transaction: Transaction,
    *,
    api_key: str | None = None,
    _client: anthropic.Anthropic | None = None,
) -> ClassificationResult:
    """Classify *transaction* using Claude.

    Args:
        transaction: Transaction to classify (read-only).
        api_key: Anthropic API key override. Falls back to
            ``ANTHROPIC_API_KEY`` environment variable.
        _client: Inject a pre-built (or mock) Anthropic client. When set,
            *api_key* is ignored. Intended for unit tests.

    Returns:
        A :class:`ClassificationResult` with ``tier_used=3``. On API or parse
        errors, returns a low-confidence result with the error in
        ``reasoning``.
    """
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
        response = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
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
        return _error_result(f"Anthropic API error: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in LLM classifier: %s", exc)
        return _error_result(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
