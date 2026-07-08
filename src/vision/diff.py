"""Pure field-level diff engine (REQ-VIS-002).

``diff_fields(legacy, vision)`` compares two normalized statement dicts and
returns a :class:`DiffReport`. Each field is classified as one of:

* ``match``       — present on both sides with equal value
* ``mismatch``    — present on both sides with differing value
* ``vision_only`` — present on the vision side only (an *extra*, allowed)
* ``legacy_only`` — present on the legacy side only (a *miss*, disqualifying)

Numeric values are compared Decimal-aware (post-quantization: ``10.5`` == ``10.50``);
everything else is compared exactly. Nested ``positions`` lists are flattened to
dotted keys (``positions[SYMBOL].price``) so the engine stays flat and pure.

``DiffReport.clean`` encodes the promotion criterion (REQ-VIS-003): *equal or
better* — zero mismatches AND zero ``legacy_only`` misses (``vision_only`` extras
are allowed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

MATCH = "match"
MISMATCH = "mismatch"
VISION_ONLY = "vision_only"
LEGACY_ONLY = "legacy_only"


@dataclass(frozen=True)
class FieldDiff:
    """The comparison outcome for a single (flattened) field key."""

    field: str
    status: str
    legacy: Any = None
    vision: Any = None


@dataclass
class DiffReport:
    """Field-level diff of a legacy dict vs a vision dict."""

    diffs: list[FieldDiff] = field(default_factory=list)

    @property
    def n_match(self) -> int:
        return sum(1 for d in self.diffs if d.status == MATCH)

    @property
    def n_mismatch(self) -> int:
        return sum(1 for d in self.diffs if d.status == MISMATCH)

    @property
    def n_vision_only(self) -> int:
        return sum(1 for d in self.diffs if d.status == VISION_ONLY)

    @property
    def n_legacy_only(self) -> int:
        return sum(1 for d in self.diffs if d.status == LEGACY_ONLY)

    @property
    def clean(self) -> bool:
        """Equal-or-better: no mismatches and no legacy_only misses."""
        return self.n_mismatch == 0 and self.n_legacy_only == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "n_match": self.n_match,
            "n_mismatch": self.n_mismatch,
            "n_vision_only": self.n_vision_only,
            "n_legacy_only": self.n_legacy_only,
            "fields": [
                {
                    "field": d.field,
                    "status": d.status,
                    "legacy": d.legacy,
                    "vision": d.vision,
                }
                for d in self.diffs
            ],
        }


def _coerce_decimal(value: Any) -> Decimal | None:
    """Return a Decimal if *value* is numeric-looking, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        s = value.strip().replace("$", "").replace(",", "")
        if not s:
            return None
        try:
            return Decimal(s)
        except InvalidOperation:
            return None
    return None


def _values_match(a: Any, b: Any) -> bool:
    """Decimal-aware equality: numeric compared post-quantization, else exact."""
    da, db = _coerce_decimal(a), _coerce_decimal(b)
    if da is not None and db is not None:
        return da == db
    return bool(a == b)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts/lists to dotted keys.

    Position lists key by ``symbol`` when present (``positions[AAPL].price``),
    else by index. Scalars land at their prefix.
    """
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, sub in value.items():
            out.update(_flatten(sub, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for idx, elem in enumerate(value):
            if isinstance(elem, dict) and elem.get("symbol") is not None:
                marker = str(elem["symbol"]).strip().upper()
            else:
                marker = str(idx)
            out.update(_flatten(elem, f"{prefix}[{marker}]"))
    else:
        out[prefix] = value
    return out


def diff_fields(legacy: dict[str, Any], vision: dict[str, Any]) -> DiffReport:
    """Compare two normalized statement dicts and return a :class:`DiffReport`."""
    flat_legacy = _flatten(legacy)
    flat_vision = _flatten(vision)

    report = DiffReport()
    for key in sorted(set(flat_legacy) | set(flat_vision)):
        in_legacy = key in flat_legacy
        in_vision = key in flat_vision
        if in_legacy and in_vision:
            if _values_match(flat_legacy[key], flat_vision[key]):
                report.diffs.append(FieldDiff(key, MATCH, flat_legacy[key], flat_vision[key]))
            else:
                report.diffs.append(
                    FieldDiff(key, MISMATCH, flat_legacy[key], flat_vision[key])
                )
        elif in_vision:
            report.diffs.append(FieldDiff(key, VISION_ONLY, None, flat_vision[key]))
        else:
            report.diffs.append(FieldDiff(key, LEGACY_ONLY, flat_legacy[key], None))
    return report


__all__ = [
    "LEGACY_ONLY",
    "MATCH",
    "MISMATCH",
    "VISION_ONLY",
    "DiffReport",
    "FieldDiff",
    "diff_fields",
]
