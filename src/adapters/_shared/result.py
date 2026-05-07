"""Canonical ImportResult dataclass for Phase-4 adapters.

Every Phase-4 adapter returns a result that conforms to this base dataclass so
consumers can treat any adapter result uniformly.  Adapter-specific fields are
added via subclass.

Field semantics
---------------
imported          : Newly inserted rows (apply mode).
matched           : Rows whose ``account_id`` resolved to a live Account.
unmatched         : Rows that parsed but could NOT be matched to an Account.
dup_skipped       : Rows skipped because an equivalent row already exists.
errors            : Per-record error strings (genuine failures only — not warnings).
warnings          : Per-record warning strings (e.g. N/A skips that are expected).
distinct_accounts : Distinct account identifiers observed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ImportResult:
    """Canonical summary of a Phase-4 adapter import run.

    Adapters that need extra fields should subclass and add them.
    """

    imported: int = 0
    """Newly inserted rows (apply mode only)."""

    matched: int = 0
    """Rows whose ``account_id`` resolved to a live Account."""

    unmatched: int = 0
    """Rows that parsed but could not be matched to an Account."""

    dup_skipped: int = 0
    """Rows skipped because an equivalent row already exists."""

    errors: list[str] = field(default_factory=list)
    """Per-record error strings (genuine failures)."""

    warnings: list[str] = field(default_factory=list)
    """Per-record warning strings (expected non-fatal skips, e.g. N/A rows)."""

    distinct_accounts: list[str] = field(default_factory=list)
    """Distinct account identifiers (account_number / policy / contract)."""
