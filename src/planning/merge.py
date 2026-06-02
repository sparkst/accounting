"""Merge: planning defaults + live inputs + CLI overrides → final Params.

Sourcing convention (spec §4.2, REQ-PLAN-006):
  - Pool: live wins by default (live data IS reality for pool).
  - Other inputs: planning value wins by default; live is informational only.
  - --override flags trump both.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from src.planning.inputs import LiveInputs
from src.planning.params import Params


def merge_live_into(
    planning: Params, live: LiveInputs, overrides: dict[str, Any]
) -> Params:
    """Build the final Params used for a simulation.

    Order of precedence (high → low):
      1. overrides
      2. live (for pool fields only)
      3. planning
    """
    # Validate overrides early so the engineer gets a useful error.
    valid_keys = {f.name for f in dataclasses.fields(planning)}
    for k in overrides:
        if k not in valid_keys:
            raise ValueError(
                f"unknown override key {k!r}; valid keys are: {sorted(valid_keys)}"
            )

    # Step 1: pool from live.
    merged = dataclasses.replace(
        planning,
        pool_taxable=live.pool_taxable,
        pool_retirement=live.pool_retirement,
    )
    # Step 2: apply overrides.
    if overrides:
        merged = dataclasses.replace(merged, **overrides)
    return merged
