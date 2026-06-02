"""Retirement & Business Sustainability Planning Engine.

See docs/superpowers/specs/2026-06-01-planning-engine-design.md for design.

Public API:
    simulate, simulate_grid, Results — engine
    Params, DEFAULTS, Scenario, ScenarioGrid — params
"""
from src.planning.engine import Results, simulate, simulate_grid
from src.planning.params import DEFAULTS, Params, Scenario, ScenarioGrid

__all__ = [
    "DEFAULTS",
    "Params",
    "Results",
    "Scenario",
    "ScenarioGrid",
    "simulate",
    "simulate_grid",
]
