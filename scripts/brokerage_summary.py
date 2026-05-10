#!/usr/bin/env python3
"""CLI shim — Option 1 brokerage summary report.

Direct invocation: `python scripts/brokerage_summary.py`
Module invocation: `python -m scripts.brokerage_summary`

Both work because:
1. PROJECT_ROOT is added to sys.path before the import, so `src.reports.*`
   resolves on direct invocation (when sys.path[0] is the scripts/ dir).
2. The filename is a valid Python identifier (no dash), so module invocation
   parses the import path correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.reports.brokerage_summary import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
