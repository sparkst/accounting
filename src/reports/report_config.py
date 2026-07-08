"""Shared YAML config loader for the reporting suite (WBR/TXF/SEL).

REQ-ID: shared architecture, reporting-suite design spec §2.

- ``safe_load`` only — no arbitrary YAML tags.
- Deep-merge onto coded defaults: a config file only needs to specify the
  keys it wants to override; anything missing falls back to the caller's
  defaults dict, and the caller can decide whether to print a
  "(defaults)" marker when ``used_defaults`` is True (whole file missing).
- Decimal coercion happens at the *caller* boundary via :func:`to_decimal` —
  this module never guesses which YAML keys are money vs plain ints (day
  counts, percentages-as-ints, etc. all coexist in the same files).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "CONFIG_DIR",
    "ConfigLoadResult",
    "deep_merge",
    "load_yaml",
    "load_config",
    "to_decimal",
]

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping from *path*. Missing file -> {}. Non-mapping top
    level or malformed YAML -> raises ValueError (never silently ignored,
    so a typo'd config doesn't quietly fall back to defaults)."""
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML — {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return data


def deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overrides* onto *defaults*, returning a new dict.
    Neither input is mutated."""
    out = dict(defaults)
    for key, value in overrides.items():
        base = out.get(key)
        if isinstance(value, dict) and isinstance(base, dict):
            out[key] = deep_merge(base, value)
        else:
            out[key] = value
    return out


class ConfigLoadResult:
    """A merged config plus whether it fell back entirely to coded defaults
    (config file absent) — drives the "(defaults)" marker in report footers."""

    __slots__ = ("data", "used_defaults", "path")

    def __init__(self, data: dict[str, Any], used_defaults: bool, path: Path) -> None:
        self.data = data
        self.used_defaults = used_defaults
        self.path = path

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


def load_config(filename: str, defaults: dict[str, Any], *, config_dir: Path | None = None) -> ConfigLoadResult:
    """Load ``<config_dir>/<filename>`` and deep-merge onto *defaults*.

    Returns a :class:`ConfigLoadResult`. ``used_defaults=True`` means the
    file didn't exist at all (pure coded defaults); a partial override file
    still merges but is not flagged as "defaults" since the operator did
    configure something.
    """
    directory = config_dir or CONFIG_DIR
    path = directory / filename
    overrides = load_yaml(path)
    used_defaults = not path.exists()
    merged = deep_merge(defaults, overrides) if overrides else dict(defaults)
    return ConfigLoadResult(merged, used_defaults, path)


def to_decimal(value: Any) -> Decimal:
    """Decimal coercion at the YAML boundary — always via str(), never a
    float constructor, per CLAUDE.md's Float -> Decimal rule."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
