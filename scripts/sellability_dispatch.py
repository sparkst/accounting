#!/usr/bin/env python3
"""Sellability metrics dispatch (REQ-SEL-001..002).

DRY-RUN by default — renders the report to stdout, no network, no ledger
writes. Pass --apply to send via Resend and record in ``alert_dispatch``.

Intended for the monthly-1st 06:30 America/Los_Angeles systemd timer
(``accounting-sellability.timer``) on the Hetzner box — prior-month scope.
On-demand: ``python -m scripts.sellability_dispatch [--date YYYY-MM-DD]``.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.reports.sellability import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
