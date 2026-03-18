"""LLMUsageLog ORM model — one row per Claude API call made by the classifier."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base

# Cost per million tokens (Claude Sonnet pricing)
_INPUT_COST_PER_M = 3.0   # $3.00 / 1M input tokens
_OUTPUT_COST_PER_M = 15.0  # $15.00 / 1M output tokens


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Return the estimated USD cost for a single Claude API call.

    Uses Claude Sonnet pricing: $3/1M input tokens, $15/1M output tokens.
    """
    return (input_tokens * _INPUT_COST_PER_M + output_tokens * _OUTPUT_COST_PER_M) / 1_000_000


class LLMUsageLog(Base):
    """Audit log for every Claude API call made during Tier 3 classification.

    Enables the health dashboard to show monthly call counts, token usage,
    and estimated spend so runaway costs can be caught early.
    """

    __tablename__ = "llm_usage_log"

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── When ──────────────────────────────────────────────────────────────────
    timestamp: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
        index=True,
        comment="UTC timestamp of the API call",
    )

    # ── What was called ───────────────────────────────────────────────────────
    model: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Claude model ID e.g. claude-3-5-haiku-20241022",
    )

    # ── Usage ─────────────────────────────────────────────────────────────────
    input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Number of input/prompt tokens billed",
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Number of output/completion tokens billed",
    )
    cost_estimate: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default="0",
        comment="Estimated USD cost based on published token pricing",
    )

    # ── Performance ───────────────────────────────────────────────────────────
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Wall-clock latency of the API call in milliseconds",
    )

    def __repr__(self) -> str:
        return (
            f"<LLMUsageLog model={self.model} "
            f"in={self.input_tokens} out={self.output_tokens} "
            f"cost=${self.cost_estimate:.6f}>"
        )
