"""Typed loader for ``config/investment_policy.yaml`` (REQ-IPD-002, REQ-BBT).

Decimal is applied at the YAML boundary — every numeric leaf goes through
``Decimal(str(x))`` so no float ever enters the concentration/glide/excise math.
The glide-line helper is the single implementation used by the policy endpoint
and the drift dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "investment_policy.yaml"


def _dec(value: Any) -> Decimal:
    """Decimal at the boundary — never ``Decimal(float)``."""
    return Decimal(str(value))


def _parse_month(value: Any) -> date:
    """Parse ``YYYY-MM`` (or a full ``YYYY-MM-DD``) into the first-of-month date."""
    s = str(value)
    parts = s.split("-")
    if len(parts) < 2:
        raise ValueError(
            f"Month value must be YYYY-MM or YYYY-MM-DD, got: {s}"
        )
    try:
        year = int(parts[0])
        month = int(parts[1])
    except (ValueError, IndexError) as exc:
        raise ValueError(
            f"Invalid month format: {s} (expected YYYY-MM or YYYY-MM-DD)"
        ) from exc
    return date(year, month, 1)


@dataclass(frozen=True)
class ConcentrationPolicy:
    symbols: list[str]
    baseline_pct: Decimal
    baseline_month: date
    target_pct: Decimal
    target_month: date
    drift_alert_threshold_pts: Decimal


@dataclass(frozen=True)
class BoldBetPosition:
    symbol: str
    thesis: str | None
    exit: str | None


@dataclass(frozen=True)
class BoldBetsPolicy:
    cap: Decimal
    positions: list[BoldBetPosition]

    @property
    def symbols(self) -> list[str]:
        return [p.symbol for p in self.positions]


@dataclass(frozen=True)
class WaExciseYear:
    threshold: Decimal
    surcharge_threshold: Decimal


@dataclass(frozen=True)
class PolicyConfig:
    concentration: ConcentrationPolicy
    international_target_pct_of_equity: Decimal
    international_symbols: list[str]
    cash_symbols: list[str]
    wa_excise: dict[int, WaExciseYear]
    bold_bets: BoldBetsPolicy

    def wa_excise_for_year(self, year: int) -> WaExciseYear | None:
        """Exact-year thresholds, else the most-recent configured year ≤ ``year``."""
        if year in self.wa_excise:
            return self.wa_excise[year]
        prior = [y for y in self.wa_excise if y <= year]
        if prior:
            return self.wa_excise[max(prior)]
        return None


def months_between(start: date, end: date) -> int:
    """Whole calendar months from ``start`` to ``end`` (may be negative)."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def glide_pct(concentration: ConcentrationPolicy, month: date) -> Decimal:
    """Linear glide from ``baseline_pct`` to ``target_pct`` over the config span.

    ``glide(m) = baseline − (baseline − target) × months_since(baseline)/span``,
    clamped to ``baseline`` before the baseline month and ``target`` after the
    target month. All-Decimal.
    """
    span = months_between(concentration.baseline_month, concentration.target_month)
    elapsed = months_between(concentration.baseline_month, month)
    if span <= 0 or elapsed <= 0:
        return concentration.baseline_pct
    if elapsed >= span:
        return concentration.target_pct
    drop = concentration.baseline_pct - concentration.target_pct
    return concentration.baseline_pct - drop * Decimal(elapsed) / Decimal(span)


def load_policy_config(path: str | Path | None = None) -> PolicyConfig:
    """Load and validate the investment-policy config, Decimal at the boundary."""
    p = Path(path) if path is not None else _DEFAULT_PATH

    try:
        raw = yaml.safe_load(p.read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Config must be a YAML dict, got {type(raw).__name__}")
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {p}: {exc}") from exc

    if "concentration" not in raw:
        raise ValueError(f"Missing required key 'concentration' in {p}")
    conc = raw["concentration"]
    if not isinstance(conc, dict):
        raise ValueError(f"'concentration' must be a dict in {p}, got {type(conc).__name__}")
    # P3-412: nested required keys raise a clear ValueError naming the key and
    # file, never a bare KeyError.
    def _req(d: dict[str, Any], key: str, section: str) -> Any:
        if key not in d:
            raise ValueError(f"Missing required key '{section}.{key}' in {p}")
        return d[key]

    concentration = ConcentrationPolicy(
        symbols=[str(s).upper() for s in _req(conc, "symbols", "concentration")],
        baseline_pct=_dec(_req(conc, "baseline_pct", "concentration")),
        baseline_month=_parse_month(_req(conc, "baseline_month", "concentration")),
        target_pct=_dec(_req(conc, "target_pct", "concentration")),
        target_month=_parse_month(_req(conc, "target_month", "concentration")),
        drift_alert_threshold_pts=_dec(
            _req(conc, "drift_alert_threshold_pts", "concentration")
        ),
    )

    wa_excise: dict[int, WaExciseYear] = {}
    for year, thresholds in (raw.get("wa_excise") or {}).items():
        if not isinstance(thresholds, dict):
            raise ValueError(f"'wa_excise.{year}' must be a dict in {p}")
        wa_excise[int(year)] = WaExciseYear(
            threshold=_dec(_req(thresholds, "threshold", f"wa_excise.{year}")),
            surcharge_threshold=_dec(
                _req(thresholds, "surcharge_threshold", f"wa_excise.{year}")
            ),
        )

    bold = raw.get("bold_bets") or {}
    positions: list[BoldBetPosition] = []
    for symbol, meta in (bold.get("symbols") or {}).items():
        meta = meta or {}
        positions.append(
            BoldBetPosition(
                symbol=str(symbol).upper(),
                thesis=(str(meta["thesis"]) if meta.get("thesis") is not None else None),
                exit=(str(meta["exit"]) if meta.get("exit") is not None else None),
            )
        )
    bold_bets = BoldBetsPolicy(cap=_dec(bold.get("cap", 20000)), positions=positions)

    if "international_target_pct_of_equity" not in raw:
        raise ValueError(f"Missing required key 'international_target_pct_of_equity' in {p}")

    return PolicyConfig(
        concentration=concentration,
        international_target_pct_of_equity=_dec(raw["international_target_pct_of_equity"]),
        international_symbols=[str(s).upper() for s in raw.get("international_symbols", [])],
        cash_symbols=[str(s).upper() for s in raw.get("cash_symbols", [])],
        wa_excise=wa_excise,
        bold_bets=bold_bets,
    )


__all__ = [
    "BoldBetPosition",
    "BoldBetsPolicy",
    "ConcentrationPolicy",
    "PolicyConfig",
    "WaExciseYear",
    "glide_pct",
    "load_policy_config",
    "months_between",
]
