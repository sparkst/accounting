"""Launchd entry point for the monthly planning run.

Invoked by com.sparkry.planning-monthly.plist on the 1st of each month at 06:00.
Just calls the CLI with --source scheduled so the persisted row is tagged
correctly.
"""
from __future__ import annotations

import sys

from src.planning.cli import main


def run() -> int:
    return main(["simulate", "--source", "scheduled"])


if __name__ == "__main__":
    sys.exit(run())
