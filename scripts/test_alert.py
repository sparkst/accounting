"""REQ-HM-014: one Resend email per failed unit per hour (dedup), no recursion."""
from unittest.mock import MagicMock

from scripts import alert as alert_mod


def test_sends_once_then_dedups(monkeypatch, tmp_path):
    sent = []
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: sent.append(payload) or {"id": "e1"}
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")

    rc1 = alert_mod.send_alert("accounting-api.service")
    rc2 = alert_mod.send_alert("accounting-api.service")  # same unit+hour → skip
    assert rc1 == 0 and rc2 == 0
    assert len(sent) == 1  # deduped


def test_distinct_units_both_send(monkeypatch, tmp_path):
    sent = []
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: sent.append(payload) or {"id": "e"}
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")
    alert_mod.send_alert("accounting-api.service")
    alert_mod.send_alert("caddy.service")
    assert len(sent) == 2


def test_send_failure_returns_nonzero(monkeypatch, tmp_path):
    fake = MagicMock()
    fake.emails.send.side_effect = RuntimeError("resend down")
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")
    rc = alert_mod.send_alert("accounting-api.service")
    assert rc == 1  # real send failure surfaces to systemd
    # a failed send must NOT write a dedup sentinel (so a retry can still alert)
    assert not (tmp_path / "alert-accounting-api.service-2026060114.sent").exists()
