"""REQ-HM-014: uptime_check.sh exit-code contract.

Drives the script with a stub ``curl`` (via CURL_BIN) that emits a canned
``<body>\\n<code>`` payload, so the test is hermetic (no network). Verifies the
healthy case exits 0 and the unhealthy cases (5xx, CF challenge 403, body
missing ok) exit non-zero so systemd OnFailure fires.
"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "uptime_check.sh"


def _run(tmp_path: Path, fake_response: str):
    # Stub curl: ignore args, print the canned response file verbatim.
    resp_file = tmp_path / "resp.txt"
    resp_file.write_text(fake_response)
    stub = tmp_path / "curl"
    stub.write_text('#!/bin/sh\ncat "$FAKE_RESP_FILE"\n')
    stub.chmod(0o755)
    env = {
        **os.environ,
        "CURL_BIN": str(stub),
        "FAKE_RESP_FILE": str(resp_file),
        "CF_ACCESS_UPTIME_CLIENT_ID": "x.access",
        "CF_ACCESS_UPTIME_CLIENT_SECRET": "y",
        "HEALTHCHECK_PING_URL": "",
    }
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def test_healthy_exits_zero(tmp_path):
    r = _run(tmp_path, '{"ok":true}\n200')
    assert r.returncode == 0, r.stderr
    assert "uptime ok" in r.stdout


def test_server_error_exits_nonzero(tmp_path):
    r = _run(tmp_path, "internal error\n500")
    assert r.returncode != 0
    assert "uptime FAIL" in r.stderr


def test_cloudflare_challenge_exits_nonzero(tmp_path):
    r = _run(tmp_path, "<html>Just a moment...</html>\n403")
    assert r.returncode != 0


def test_200_but_body_not_ok_exits_nonzero(tmp_path):
    # 200 with an unexpected body must still fail (don't trust the code alone).
    r = _run(tmp_path, '{"ok":false}\n200')
    assert r.returncode != 0
