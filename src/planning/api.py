"""GET /api/planning/runs/latest — read-only single endpoint (REQ-PLAN-011)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.planning.models import PlanningRun

router = APIRouter(prefix="/planning", tags=["planning"])


@router.get("/runs/latest")
def get_latest_run(db: Session = Depends(get_db)) -> dict[str, Any]:  # noqa: B008
    """Return the most recent PlanningRun row ordered by run_at descending.

    REQ-PLAN-011: single endpoint exposing the latest simulation result.
    """
    row = (
        db.query(PlanningRun)
        .order_by(PlanningRun.run_at.desc())
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no planning runs yet")
    return {
        "id": row.id,
        "run_at": row.run_at.isoformat(),
        "source": row.source,
        "params": row.params_json,
        "live_inputs": row.live_inputs_json,
        "scenarios": row.scenarios_json,
        "notes": row.notes,
    }
