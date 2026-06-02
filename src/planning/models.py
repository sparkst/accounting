"""PlanningRun SQLAlchemy model.

One row per simulate() invocation. JSON columns (not normalized) — consumers
are humans reading reports and future Claude analyses; flexibility beats query
efficiency for ~12 scheduled runs/year + ad-hoc volume.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models.base import Base

VALID_SOURCES = frozenset({"cli", "scheduled", "api"})


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class PlanningRun(Base):
    """One row per simulate() invocation.

    JSON columns store:
    - params_json: params object passed to simulate()
    - live_inputs_json: live inputs merged in (from fixture or loader)
    - scenarios_json: scenario grid results
    """

    __tablename__ = "planning_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    live_inputs_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    scenarios_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
        onupdate=_now,
    )
