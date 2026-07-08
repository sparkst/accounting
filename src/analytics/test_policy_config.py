"""Tests for the investment-policy config loader + glide math (REQ-IPD-002)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.analytics.policy_config import (
    glide_pct,
    load_policy_config,
)


def test_load_default_config() -> None:
    cfg = load_policy_config()
    assert cfg.concentration.symbols == ["AMZN", "MSFT"]
    assert cfg.concentration.baseline_pct == Decimal("51")
    assert cfg.concentration.target_pct == Decimal("35")
    assert cfg.concentration.baseline_month == date(2026, 7, 1)
    assert cfg.concentration.target_month == date(2031, 7, 1)
    assert isinstance(cfg.concentration.baseline_pct, Decimal)


def test_glide_at_baseline_is_baseline() -> None:
    """REQ-IPD-002: glide at the baseline month equals the baseline pct."""
    cfg = load_policy_config()
    assert glide_pct(cfg.concentration, date(2026, 7, 1)) == Decimal("51")


def test_glide_before_baseline_clamps_to_baseline() -> None:
    cfg = load_policy_config()
    assert glide_pct(cfg.concentration, date(2026, 1, 1)) == Decimal("51")


def test_glide_midpoint() -> None:
    """REQ-IPD-002: 30 months in (half of 60) → halfway between 51 and 35 = 43."""
    cfg = load_policy_config()
    got = glide_pct(cfg.concentration, date(2029, 1, 1))  # 30 months after 2026-07
    assert got == Decimal("43")


def test_glide_clamps_at_target_after_target_month() -> None:
    """REQ-IPD-002: at/after the target month the glide clamps to 35%."""
    cfg = load_policy_config()
    assert glide_pct(cfg.concentration, date(2031, 7, 1)) == Decimal("35")
    assert glide_pct(cfg.concentration, date(2033, 1, 1)) == Decimal("35")


def test_wa_excise_thresholds() -> None:
    """REQ-IPD-003: per-tax-year thresholds parse as Decimal."""
    cfg = load_policy_config()
    year = cfg.wa_excise_for_year(2026)
    assert year is not None
    assert year.threshold == Decimal("270000")
    assert year.surcharge_threshold == Decimal("1000000")
    # Fallback to the most-recent configured year for an unlisted future year.
    assert cfg.wa_excise_for_year(2099) is not None


def test_bold_bets_cap_and_notes() -> None:
    """REQ-BBT-002: cap defaults 20000; positions carry thesis/exit text."""
    cfg = load_policy_config()
    assert cfg.bold_bets.cap == Decimal("20000")
    assert "TSLA" in cfg.bold_bets.symbols
    tsla = next(p for p in cfg.bold_bets.positions if p.symbol == "TSLA")
    assert tsla.thesis
    assert tsla.exit
