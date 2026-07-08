"""Per-name effective-cutoff predicate for networth-history dedup.

REQ-FIX-WLT-004 (wealth design §4.2). Ports the sparkry-crm D1 ``unmatchedActiveAt``
algorithm (REQ-WD-009) verbatim so the local FastAPI net-worth series and the
Cloudflare Worker agree row-for-row. Extracted here so it is unit-testable in
isolation against the shared parity fixture.

A legacy (unmatched) ``AccountBalanceSnapshot`` row contributes to the net-worth
total only *strictly before* its effective cutoff — the earlier of:

  * Tier 1: ``matched_first`` — first date a *matched* ABS carries the same raw
    name (the live rollup started reporting under the same label); and
  * Tier 2: ``alias_cutoff`` — earliest ``PositionSnapshot.as_of`` of the
    account this raw name is explicitly aliased to.

An absent map entry means +∞ for that tier (no suppression from it). Both absent
⇒ no cutoff at all ⇒ full history included. Every map is keyed on the LOWERCASED
raw name (REQ-WD-009 P1-B key-casing contract).
"""

from __future__ import annotations

from datetime import date


def effective_cutoff(
    raw_name: str,
    matched_first: dict[str, date],
    alias_cutoff: dict[str, date],
) -> date | None:
    """Return the effective cutoff date for ``raw_name``, or None if uncut.

    The earlier of the tier-1 (matched-name first date) and tier-2 (alias
    cutoff) entries; None when neither map has an entry for the lowercased name.
    """
    key = raw_name.lower()
    candidates = [d for d in (matched_first.get(key), alias_cutoff.get(key)) if d is not None]
    return min(candidates) if candidates else None


def unmatched_active_at(
    raw_name: str,
    target: date,
    matched_first: dict[str, date],
    alias_cutoff: dict[str, date],
) -> bool:
    """True when the unmatched ``raw_name`` row still contributes at ``target``.

    Include strictly *before* the effective cutoff; exclude (contribute $0,
    including any carry-forward) at or after it. No cutoff ⇒ always active.
    """
    cutoff = effective_cutoff(raw_name, matched_first, alias_cutoff)
    if cutoff is None:
        return True
    return target < cutoff


__all__ = ["effective_cutoff", "unmatched_active_at"]
