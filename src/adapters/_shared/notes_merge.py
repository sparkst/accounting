"""Merge machine-generated note blocks into human-curated ``Account.notes``.

REQ-FIX-WLT-009. Adapters that record structured fields in ``Account.notes``
must never clobber the operator's free text. This helper owns exactly one
delimited machine block, appended below a marker line::

    <human notes preserved verbatim>
    --- [na_iul auto 2026-07-07] accumulation=…; premium_paid=…; cost_basis=…

On re-import the block is replaced in place (idempotent for pure-machine
updates); text above the marker is never touched.
"""

from __future__ import annotations


def machine_block(marker: str, date_iso: str, body: str) -> str:
    """Render a single machine block line: ``--- [<marker> <date>] <body>``."""
    return f"--- [{marker} {date_iso}] {body}"


def merge_machine_block(notes: str | None, marker: str, block: str) -> str:
    """Merge ``block`` into ``notes``, replacing any existing block for ``marker``.

    Args:
        notes:  Existing (possibly human-curated) notes, or None.
        marker: Stable detection family (e.g. ``"na_iul auto"``) — WITHOUT the
                leading ``--- [``. Any existing content from the first line
                starting with ``--- [<marker>`` through end-of-string is treated
                as the machine block and replaced.
        block:  The full machine block text to write (typically from
                :func:`machine_block`).

    Returns:
        The merged notes string. Human text above the marker is preserved
        verbatim; a pure-machine note round-trips idempotently.
    """
    sep_prefix = f"--- [{marker}"
    human = notes or ""
    idx = human.find(sep_prefix)
    if idx != -1:
        human = human[:idx]
    human = human.rstrip()
    if human:
        return f"{human}\n{block}"
    return block


__all__ = ["machine_block", "merge_machine_block"]
