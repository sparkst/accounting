"""REQ-HM-014: external script exits 1 below 5 GB, 0 when ample."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "disk_check.sh"


def test_exits_0_when_ample():
    r = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True,
                       env={**os.environ, "DISK_FREE_GB_OVERRIDE": "50"})
    assert r.returncode == 0


def test_exits_1_when_constrained():
    r = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True,
                       env={**os.environ, "DISK_FREE_GB_OVERRIDE": "3"})
    assert r.returncode == 1
