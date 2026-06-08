"""Tests for the alerts_dispatch CLI.

REQ-ID: REQ-ALERT-007 (DRY-RUN is the default; --apply opts in)
"""

from datetime import date

import pytest

import scripts.alerts_dispatch as cli
from src.alerts.dispatcher import DispatchSummary


class _DummySession:
    def close(self) -> None:
        pass


def test_default_is_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_dispatch(session: object, today: date, *, apply: bool) -> DispatchSummary:
        captured["apply"] = apply
        captured["today"] = today
        return DispatchSummary(dry_run=2)

    monkeypatch.setattr(cli, "dispatch_alerts", _fake_dispatch)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", _DummySession)

    rc = cli.main(["--date", "2026-05-10"])
    assert rc == 0
    assert captured["apply"] is False
    assert captured["today"] == date(2026, 5, 10)


def test_apply_flag_enables_send(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_dispatch(session: object, today: date, *, apply: bool) -> DispatchSummary:
        captured["apply"] = apply
        return DispatchSummary(sent=1)

    monkeypatch.setattr(cli, "dispatch_alerts", _fake_dispatch)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", _DummySession)

    rc = cli.main(["--apply", "--date", "2026-05-10"])
    assert rc == 0
    assert captured["apply"] is True


def test_failed_alerts_return_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_dispatch(session: object, today: date, *, apply: bool) -> DispatchSummary:
        return DispatchSummary(failed=1)

    monkeypatch.setattr(cli, "dispatch_alerts", _fake_dispatch)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", _DummySession)

    rc = cli.main(["--apply", "--date", "2026-05-10"])
    assert rc == 1
