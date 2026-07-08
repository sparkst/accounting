#!/usr/bin/env python3
"""Weekly Business Review scorecard dispatch (REQ-WBR-001..003).

DRY-RUN by default — renders the report to stdout, no network, no ledger
writes. Pass --apply to send via Resend and record in ``alert_dispatch``.

Intended for the Mon 06:00 America/Los_Angeles systemd timer
(``accounting-wbr.timer``) on the Hetzner box. On-demand:
``python -m scripts.wbr_dispatch [--date YYYY-MM-DD]``.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.reports.wbr import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
