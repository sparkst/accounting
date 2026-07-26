#!/usr/bin/env python3
"""Scheduled sync for the API-key-backed adapters (Stripe, Shopify).

REQ-ID: REQ-FIX-ING-020

Stripe and Shopify have no sync timer of their own. Both last ran 2026-06-08
— the day after the Plaid cutover, which appears to have dropped whatever was
triggering them — and the six-week gap went unnoticed until it surfaced as
$431 of missing June revenue during the WA B&O filing review. Silence is the
failure mode this script exists to make impossible.

Rather than re-implement adapter dispatch, this POSTs to the already-tested
``/api/ingest/run`` endpoint, which owns adapter dispatch, classification, and
the single-ingest concurrency lock. This script's only job is to turn that
endpoint's *response body* into a process exit code systemd can alert on.

That translation is the whole point: ``/api/ingest/run`` deliberately returns
HTTP 200 with per-step problems collected in ``errors`` / ``records_failed``
so the dashboard can render partial results. A bare ``curl`` in the unit file
would therefore exit 0 on a total adapter failure and the ``OnFailure=`` alert
would never fire — the exact silent-failure shape that caused the six-week gap.

Usage:
    python -m scripts.adapter_sync --source stripe            # dry-run
    python -m scripts.adapter_sync --source stripe --apply    # really sync
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("adapter_sync")

DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: Sources this script is allowed to drive. Deliberately NOT the full Source
#: enum: Plaid has its own dedicated sync units, and re-running it from here
#: would double-drive the cursor.
SUPPORTED_SOURCES = ("stripe", "shopify")

#: Adapter errors that are known, understood, and NOT worth waking anyone for.
#: Anything not matched here fails the run.
#:
#: A daily alert nobody can action trains the reader to ignore the channel, so
#: the alert stops working for the failures that DO matter. Each entry is a
#: standing decision to tolerate a specific known condition, and each carries
#: the exit criterion that should delete it.
#:
#: - Shopify payouts 403: the private app's token lacks
#:   ``read_shopify_payments_payouts``. Harmless because payout *settlement*
#:   reaches the register via Plaid's bank feed anyway, and the orders — the
#:   actual revenue — ingest normally. REMOVE THIS ENTRY once the scope is
#:   approved, so a genuine future payout failure is no longer swallowed.
BENIGN_ERROR_PATTERNS: tuple[str, ...] = (
    "read_shopify_payments_payouts scope not approved",
)


class SyncError(RuntimeError):
    """Raised when the ingest run failed in a way that warrants an alert."""


@dataclass
class SyncOutcome:
    """What one adapter run did, reduced to what a human needs to see."""

    source: str
    ingested: int
    classified: int
    needs_review: int
    records_failed: int
    fatal_errors: list[str]
    benign_errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.fatal_errors and self.records_failed == 0


def is_benign(message: str) -> bool:
    """True when this adapter error is a known-tolerated condition."""
    return any(pattern in message for pattern in BENIGN_ERROR_PATTERNS)


def summarize(source: str, payload: dict[str, Any]) -> SyncOutcome:
    """Reduce an ``IngestSummary`` body to a pass/fail outcome.

    ``records_failed`` is summed across adapter_results rather than read from a
    top-level field: the endpoint reports failures per adapter, and a run that
    created rows while failing others is still a failure worth alerting on.
    """
    errors = [str(e) for e in payload.get("errors") or []]
    adapter_results = payload.get("adapter_results") or []
    records_failed = sum(int(r.get("records_failed") or 0) for r in adapter_results)
    return SyncOutcome(
        source=source,
        ingested=int(payload.get("ingested_count") or 0),
        classified=int(payload.get("classified_count") or 0),
        needs_review=int(payload.get("needs_review_count") or 0),
        records_failed=records_failed,
        fatal_errors=[e for e in errors if not is_benign(e)],
        benign_errors=[e for e in errors if is_benign(e)],
        warnings=[str(w) for w in payload.get("warnings") or []],
    )


def run_sync(
    source: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str,
    timeout: float = 300.0,
    client: httpx.Client | None = None,
) -> SyncOutcome:
    """POST to /api/ingest/run and translate the response into an outcome.

    A 409 means another ingest holds the lock. That is treated as a failure,
    not as a benign skip: with the timers spaced apart it should never happen,
    so it means either a previous run hung or the schedule drifted — both
    things a human needs to know about, and both of which would otherwise
    present as "the sync silently stopped running" all over again.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.post(
            f"{base_url}/api/ingest/run",
            params={"source": source},
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise SyncError(f"{source}: request to {base_url} failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if response.status_code == 409:
        raise SyncError(
            f"{source}: another ingest is already running (HTTP 409) — a prior "
            "run may be hung, or two timers overlapped."
        )
    if response.status_code >= 400:
        raise SyncError(
            f"{source}: ingest endpoint returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SyncError(
            f"{source}: ingest endpoint returned non-JSON: {response.text[:300]}"
        ) from exc
    return summarize(source, payload)


def _report(outcome: SyncOutcome) -> None:
    logger.info(
        "%s: ingested=%d classified=%d needs_review=%d failed=%d",
        outcome.source,
        outcome.ingested,
        outcome.classified,
        outcome.needs_review,
        outcome.records_failed,
    )
    for warning in outcome.warnings:
        logger.warning("%s: %s", outcome.source, warning)
    for message in outcome.benign_errors:
        # Logged at WARNING, never suppressed: tolerated is not the same as
        # invisible, and this line is the breadcrumb for deleting the
        # allowlist entry once the underlying condition is fixed.
        logger.warning("%s: tolerated known issue: %s", outcome.source, message)
    for message in outcome.fatal_errors:
        logger.error("%s: %s", outcome.source, message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=SUPPORTED_SOURCES)
    parser.add_argument(
        "--apply", action="store_true", help="Really sync (default: dry-run)."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.apply:
        print(
            f"DRY-RUN: would POST {args.base_url}/api/ingest/run?source={args.source}. "
            "Re-run with --apply to sync."
        )
        return 0

    api_key = os.environ.get("API_KEY")
    if not api_key:
        # Fail loudly rather than sending an unauthenticated request that would
        # 401 and read as an endpoint problem.
        logger.error("API_KEY is not set in the environment")
        return 2

    try:
        outcome = run_sync(
            args.source,
            base_url=args.base_url,
            api_key=api_key,
            timeout=args.timeout,
        )
    except SyncError as exc:
        logger.error("%s", exc)
        return 1

    _report(outcome)
    if not outcome.ok:
        logger.error(
            "%s: sync FAILED — %d record(s) failed, %d fatal error(s)",
            outcome.source,
            outcome.records_failed,
            len(outcome.fatal_errors),
        )
        return 1
    print(
        f"{args.source}: OK — ingested {outcome.ingested}, "
        f"classified {outcome.classified}, needs_review {outcome.needs_review}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
