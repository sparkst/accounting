"""REQ-HM-014: one Resend email per failed unit per hour (dedup), no recursion.

REQ-FIX-ALR-006: journal-tail + failed-alert-ledger enrichment of the body.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts import alert as alert_mod


def _record_send(sent: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, str]:
    sent.append(payload)
    return {"id": "e1"}


def test_sends_once_then_dedups(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: _record_send(sent, payload)
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")

    rc1 = alert_mod.send_alert("accounting-api.service")
    rc2 = alert_mod.send_alert("accounting-api.service")  # same unit+hour → skip
    assert rc1 == 0 and rc2 == 0
    assert len(sent) == 1  # deduped


def test_distinct_units_both_send(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: _record_send(sent, payload)
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")
    alert_mod.send_alert("accounting-api.service")
    alert_mod.send_alert("caddy.service")
    assert len(sent) == 2


def test_send_failure_returns_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = MagicMock()
    fake.emails.send.side_effect = RuntimeError("resend down")
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")
    rc = alert_mod.send_alert("accounting-api.service")
    assert rc == 1  # real send failure surfaces to systemd
    # a failed send must NOT write a dedup sentinel (so a retry can still alert)
    assert not (tmp_path / "alert-accounting-api.service-2026060114.sent").exists()


# ── REQ-FIX-ALR-006: journal tail + failed-ledger enrichment ────────────────


def test_body_includes_journal_tail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: _record_send(sent, payload)
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setattr(alert_mod, "_journal_tail", lambda unit: "line1\nline2\nline3")
    monkeypatch.setattr(alert_mod, "_failed_alert_subjects", lambda days=2: [])
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")

    rc = alert_mod.send_alert("accounting-api.service")
    assert rc == 0
    body = sent[0]["text"]
    assert "line1\nline2\nline3" in body
    assert "journal lines" in body


def test_journalctl_raising_still_sends_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A journalctl failure (missing binary, permission denied, timeout, ...)
    must never block the basic alert email. Exercises the REAL _journal_tail
    (only the underlying subprocess.run is faked) so the swallowing behavior
    itself is under test, not a mock standing in for it."""
    sent: list[dict[str, Any]] = []
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: _record_send(sent, payload)
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setattr(alert_mod, "_failed_alert_subjects", lambda days=2: [])

    def _boom(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("journalctl not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")

    rc = alert_mod.send_alert("accounting-api.service")
    assert rc == 0
    assert len(sent) == 1
    assert "journal lines" not in sent[0]["text"]  # no tail available, gracefully omitted


def test_journal_tail_returns_none_on_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert alert_mod._journal_tail("some.service") is None


def test_dispatcher_unit_includes_failed_alert_subjects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[dict[str, Any]] = []
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: _record_send(sent, payload)
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setattr(alert_mod, "_journal_tail", lambda unit: None)
    monkeypatch.setattr(
        alert_mod, "_failed_alert_subjects", lambda days=2: ["WA B&O due — Sparkry LLC"]
    )
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")

    rc = alert_mod.send_alert("accounting-ea-alerts.service")
    assert rc == 0
    body = sent[0]["text"]
    assert "WA B&O due — Sparkry LLC" in body
    assert "Failed alert_dispatch rows" in body


def test_non_dispatcher_unit_never_queries_failed_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"n": 0}
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: {"id": "e1"}
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setattr(alert_mod, "_journal_tail", lambda unit: None)

    def _spy(days: int = 2) -> list[str]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(alert_mod, "_failed_alert_subjects", _spy)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")

    alert_mod.send_alert("caddy.service")
    assert calls["n"] == 0
