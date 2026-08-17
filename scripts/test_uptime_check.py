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


def test_probe_sends_public_host_header(tmp_path):
    """2026-08-02..06 white-page incident: Caddy's site address was a
    127.0.0.1 HOST matcher, so the localhost probe stayed green while every
    public-Host request got an empty 200. The probe must send the public Host
    header so that failure mode trips the alert."""
    resp_file = tmp_path / "resp.txt"
    resp_file.write_text('{"ok":true}\n200')
    args_file = tmp_path / "args.txt"
    stub = tmp_path / "curl"
    stub.write_text('#!/bin/sh\necho "$@" >> "$ARGS_FILE"\ncat "$FAKE_RESP_FILE"\n')
    stub.chmod(0o755)
    env = {
        **os.environ,
        "CURL_BIN": str(stub),
        "FAKE_RESP_FILE": str(resp_file),
        "ARGS_FILE": str(args_file),
        "HEALTHCHECK_PING_URL": "",
    }
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "Host: books.sparkry.ai" in args_file.read_text()


# ---------------------------------------------------------------------------
# Issue #53: the unit is versioned in deploy/ and tolerant of slow starts.
# ---------------------------------------------------------------------------

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _kv(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{key}= missing")


def test_unit_start_timeout_tolerates_starvation_but_fits_timer_period():
    svc = (DEPLOY / "accounting-uptime-check.service").read_text()
    tmr = (DEPLOY / "accounting-uptime-check.timer").read_text()
    timeout = int(_kv(svc, "TimeoutStartSec"))
    # curl --max-time 15 (probe) + 10 (dead-man ping) = 25 s of bounded network
    # time; the rest is scheduling latency on a starved 2 vCPU box (issue #53
    # saw >60 s). Must still finish before the next 5-min timer tick.
    assert 120 <= timeout < 300
    assert _kv(tmr, "OnUnitActiveSec") == "5min"
    assert _kv(svc, "Type") == "oneshot"
    assert _kv(svc, "OnFailure") == "accounting-alert-webhook@%p.service"
    assert _kv(svc, "ExecStart").endswith("/scripts/uptime_check.sh")


def test_probe_curl_bounds_are_inside_unit_timeout():
    svc = (DEPLOY / "accounting-uptime-check.service").read_text()
    timeout = int(_kv(svc, "TimeoutStartSec"))
    script = SCRIPT.read_text()
    import re

    bounds = [int(m) for m in re.findall(r"--max-time (\d+)", script)]
    assert bounds, "uptime_check.sh must bound every curl with --max-time"
    assert sum(bounds) < timeout
