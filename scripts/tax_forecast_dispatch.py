#!/usr/bin/env python3
"""Tax-posture forecaster dispatch (REQ-TXF-001..004).

DRY-RUN by default — renders the report to stdout, no network, no ledger
writes. Pass --apply to send via Resend and record in ``alert_dispatch``.

Intended for the Jan/Apr/Jun/Sep 1st 07:00 America/Los_Angeles systemd timer
(``accounting-tax-forecast.timer``) on the Hetzner box — two weeks ahead of
each estimated-tax due date. On-demand:
``python -m scripts.tax_forecast_dispatch [--date YYYY-MM-DD]``.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.reports.tax_forecast import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
