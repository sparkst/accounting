"""Optional Gemini narrative for the monthly close (REQ-MCA-004, spec §1.5).

Renders a 5-sentence prose summary from the already-computed CloseReport
aggregates ONLY (numbers — never raw transactions). Env-gated on
``CLOSE_NARRATIVE_LLM=1``; any failure, an open circuit breaker, or the flag
being off returns ``None`` and the deterministic report still sends. Mirrors the
circuit-breaker + usage-log pattern from ``src/classification/llm_classifier.py``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

from src.models.llm_usage import LLMUsageLog, estimate_cost_for_model

if TYPE_CHECKING:
    from google import genai
    from sqlalchemy.orm import Session

    from src.close.report import CloseReport

logger = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash-lite"
_MAX_TOKENS = 400
_CB_FAILURE_THRESHOLD = 3
_CB_RECOVERY_TIMEOUT_S = 60.0

_SYSTEM_PROMPT = (
    "You are a concise CFO assistant. Given month-end close aggregates for a "
    "cash-basis accounting system, write exactly five plain-English sentences "
    "summarizing the month: activity, auto-confirm coverage, review backlog, and "
    "any reconciliation or anomaly flags worth a human's attention. Use only the "
    "numbers provided. No markdown, no bullet points, no invented figures."
)


class _CircuitState:
    """Clone of llm_classifier._CircuitState — narrative-local breaker."""

    def __init__(self) -> None:
        self.consecutive_failures: int = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= _CB_FAILURE_THRESHOLD:
            self.opened_at = time.monotonic()

    def allow_attempt(self) -> bool:
        if not self.is_open:
            return True
        assert self.opened_at is not None
        return (time.monotonic() - self.opened_at) >= _CB_RECOVERY_TIMEOUT_S


_circuit = _CircuitState()


def _reset_circuit_breaker() -> None:
    """Reset the breaker to closed. Tests only."""
    _circuit.consecutive_failures = 0
    _circuit.opened_at = None


def build_prompt(report: CloseReport) -> str:
    """Build the aggregate-only user prompt. Contains no raw transaction rows."""
    rec = report.reconcile
    discrepancies = sum(
        1 for it in rec.items for a in it.accounts if a.tie_out_ok is False
    )
    gap_items = sum(1 for it in rec.items if it.has_gap)
    an = report.anomalies
    lines = [
        f"month: {report.month}",
        f"rows_ingested: {report.rows_ingested}",
        f"auto_confirmed: {report.autoconfirm.total}",
        f"needs_review_depth: {report.needs_review_depth}",
        f"plaid_items: {len(rec.items)}",
        f"items_with_sync_gaps: {gap_items}",
        f"balance_discrepancies: {discrepancies}",
        f"stuck_pending: {len(rec.stuck_pending)}",
        f"unmatched_payouts: {len(rec.unmatched_payouts)}",
        f"new_vendors: {len(an.new_vendors)}",
        f"amount_outliers: {len(an.outliers)}",
        f"missing_recurring: {len(an.missing_recurring)}",
    ]
    return "\n".join(lines)


def _write_usage_log(session: Session | None, response: Any, duration_ms: int) -> None:
    if session is None:
        return
    try:
        raw_model = getattr(response, "model_version", None) or _MODEL
        model_name = str(raw_model) if isinstance(raw_model, str) else _MODEL
        usage = getattr(response, "usage_metadata", None)
        in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
        session.add(
            LLMUsageLog(
                model=model_name,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_estimate=estimate_cost_for_model(model_name, in_tok, out_tok),
                duration_ms=duration_ms,
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 — usage logging never breaks the caller
        logger.error("Failed to write narrative LLMUsageLog: %s", exc)


def render_narrative(
    report: CloseReport,
    *,
    _client: genai.Client | None = None,
    session: Session | None = None,
) -> str | None:
    """Return a 5-sentence narrative, or ``None`` when disabled/unavailable.

    Returns ``None`` when ``CLOSE_NARRATIVE_LLM != "1"``, when the breaker is
    open, or on any provider/parse error — the report always still sends.
    """
    if os.getenv("CLOSE_NARRATIVE_LLM") != "1":
        return None
    if not _circuit.allow_attempt():
        return None

    try:
        from google import genai
        from google.genai import types as genai_types

        client = _client or genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
        t0 = time.monotonic()
        response = client.models.generate_content(
            model=_MODEL,
            contents=build_prompt(report),
            config=genai_types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                max_output_tokens=_MAX_TOKENS,
                temperature=0.0,
            ),
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        _circuit.record_success()
        _write_usage_log(session, response, duration_ms)
        text = getattr(response, "text", None)
        if not text:
            return None
        return str(text).strip()
    except Exception as exc:  # noqa: BLE001 — narrative is additive; never gates the report
        logger.warning("Narrative generation failed: %s", exc)
        _circuit.record_failure()
        return None
