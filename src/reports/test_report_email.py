"""Tests for src/reports/report_email.py — shared Resend send+ledger path
for the reporting suite (WBR/TXF/SEL).

REQ-ID: REQ-WBR-003, REQ-TXF-004, REQ-SEL delivery ("reports record
resend_email channel + NULL payload; DRY-RUN sends nothing and writes
nothing").

P1-remail regression: this module is the single choke point every report
routes through to actually send email and record the ledger, yet had no
dedicated test file — several failure branches were entirely untested. This
file closes that gap.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
import resend as _resend
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401 — registers all ORM models before create_all
from src.alerts.models import AlertDispatch
from src.models.base import Base
from src.reports import report_email


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionLocal()
    yield s
    s.close()


# ── send_report_email ────────────────────────────────────────────────────


class TestSendReportEmail:
    def test_dry_run_never_touches_the_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(params: object) -> dict[str, str]:
            raise AssertionError("dry-run must never call resend.Emails.send")

        monkeypatch.setattr(_resend.Emails, "send", _boom)
        result = report_email.send_report_email("subj", "body", apply=False)
        assert result.status == "dry_run"
        assert result.error is None

    def test_missing_resend_api_key_returns_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        result = report_email.send_report_email("subj", "body", apply=True)
        assert result.status == "failed"
        assert result.error == "RESEND_API_KEY is not configured"

    def test_send_exception_is_caught_and_returns_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESEND_API_KEY", "test-key")

        def _raise(params: object) -> dict[str, str]:
            raise RuntimeError("Resend API unreachable")

        monkeypatch.setattr(_resend.Emails, "send", _raise)
        result = report_email.send_report_email("subj", "body", apply=True)
        assert result.status == "failed"
        assert result.error is not None
        assert "Resend API unreachable" in result.error

    def test_invalid_report_to_email_override_returns_failed_without_sending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        # CRLF-injected override — header-injection attempt via the env var.
        monkeypatch.setenv("REPORT_TO_EMAIL", "victim@example.com\r\nBcc: attacker@evil.com")

        def _boom(params: object) -> dict[str, str]:
            raise AssertionError("must not call resend.Emails.send with an invalid recipient")

        monkeypatch.setattr(_resend.Emails, "send", _boom)
        result = report_email.send_report_email("subj", "body", apply=True)
        assert result.status == "failed"
        assert result.error is not None

    def test_apply_true_calls_send_and_returns_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        calls: list[object] = []

        def _fake_send(params: object) -> dict[str, str]:
            calls.append(params)
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)
        result = report_email.send_report_email("subj", "body", apply=True)
        assert result.status == "sent"
        assert result.error is None
        assert len(calls) == 1


# ── resolve_to_email / _validate_email ───────────────────────────────────


class TestResolveToEmail:
    def test_default_used_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REPORT_TO_EMAIL", raising=False)
        assert report_email.resolve_to_email() == report_email.DEFAULT_TO_EMAIL

    def test_valid_override_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REPORT_TO_EMAIL", "someone@example.com")
        assert report_email.resolve_to_email() == "someone@example.com"

    def test_malformed_override_raises_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REPORT_TO_EMAIL", "not-an-email")
        with pytest.raises(ValueError, match="Invalid email address"):
            report_email.resolve_to_email()

    def test_crlf_injected_override_raises_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REPORT_TO_EMAIL", "victim@example.com\r\nBcc: attacker@evil.com")
        with pytest.raises(ValueError, match="forbidden characters"):
            report_email.resolve_to_email()


# ── record_dispatch ───────────────────────────────────────────────────────


class TestRecordDispatch:
    def test_insert_writes_resend_email_channel_with_null_payload(self, session: Session) -> None:
        report_email.record_dispatch(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            result=report_email.SendResult("sent"),
        )
        row = (
            session.query(AlertDispatch)
            .filter_by(alert_key="wbr:2026-W28", occurrence_date="2026-07-06")
            .one()
        )
        assert row.delivery_channel == "resend_email"
        assert row.payload_json is None
        assert row.status == "sent"

    def test_integrity_error_race_is_swallowed_via_rollback(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A concurrent insert can race the read-then-write in record_dispatch:
        the ``existing is None`` check passes, but the insert then collides
        with the unique constraint when flushed (a genuine concurrent writer
        winning the race). That IntegrityError must be caught and rolled
        back, not propagated. We force the underlying INSERT to raise by
        patching ``Session.flush`` — the call that actually executes SQL —
        once, matching how a real UNIQUE-constraint violation surfaces."""
        original_flush = session.flush
        state = {"calls": 0}

        def _flush_raising_once(objects: object = None) -> None:
            state["calls"] += 1
            if state["calls"] == 1:
                raise IntegrityError(
                    "INSERT INTO alert_dispatch ...", {}, Exception("UNIQUE constraint failed")
                )
            original_flush()

        monkeypatch.setattr(session, "flush", _flush_raising_once)

        # Must not raise.
        report_email.record_dispatch(
            session,
            alert_key="race:key",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            result=report_email.SendResult("sent"),
        )

        monkeypatch.setattr(session, "flush", original_flush)
        row = (
            session.query(AlertDispatch)
            .filter_by(alert_key="race:key", occurrence_date="2026-07-06")
            .one_or_none()
        )
        # The simulated race rolled the insert back — nothing was persisted,
        # and (critically) no exception escaped record_dispatch.
        assert row is None
        assert state["calls"] >= 1

    def test_existing_row_is_updated_in_place(self, session: Session) -> None:
        report_email.record_dispatch(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            result=report_email.SendResult("failed", "boom"),
        )
        report_email.record_dispatch(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            result=report_email.SendResult("sent"),
        )
        rows = (
            session.query(AlertDispatch)
            .filter_by(alert_key="wbr:2026-W28", occurrence_date="2026-07-06")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "sent"
        assert rows[0].error_detail is None


# ── already_sent / dispatch_report retry semantics ───────────────────────


class TestAlreadySentRetrySemantics:
    def test_failed_row_is_not_treated_as_already_sent(self, session: Session) -> None:
        report_email.record_dispatch(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            result=report_email.SendResult("failed", "RESEND_API_KEY is not configured"),
        )
        assert report_email.already_sent(session, "wbr:2026-W28", "2026-07-06") is False

    def test_sent_row_is_already_sent(self, session: Session) -> None:
        report_email.record_dispatch(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            result=report_email.SendResult("sent"),
        )
        assert report_email.already_sent(session, "wbr:2026-W28", "2026-07-06") is True

    def test_dispatch_report_retries_after_a_prior_failure_not_skips(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run that failed to send (e.g. RESEND_API_KEY missing at the
        time) must be retried by a subsequent run, not silently skipped as
        already-sent."""
        monkeypatch.delenv("RESEND_API_KEY", raising=False)

        first = report_email.dispatch_report(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            body="body",
            apply=True,
        )
        assert first.status == "failed"

        # Now fix the config and retry.
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        send_calls: list[object] = []

        def _fake_send(params: object) -> dict[str, str]:
            send_calls.append(params)
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)

        second = report_email.dispatch_report(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            body="body",
            apply=True,
        )
        # Must actually retry (call send), not be reported as "skipped".
        assert second.status == "sent"
        assert len(send_calls) == 1

        row = (
            session.query(AlertDispatch)
            .filter_by(alert_key="wbr:2026-W28", occurrence_date="2026-07-06")
            .one()
        )
        assert row.status == "sent"

    def test_dispatch_report_skips_after_a_prior_success(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        send_calls: list[object] = []

        def _fake_send(params: object) -> dict[str, str]:
            send_calls.append(params)
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)

        first = report_email.dispatch_report(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            body="body",
            apply=True,
        )
        second = report_email.dispatch_report(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            body="body",
            apply=True,
        )
        assert first.status == "sent"
        assert second.status == "skipped"
        assert len(send_calls) == 1

    def test_dry_run_never_checks_or_writes_ledger(self, session: Session) -> None:
        result = report_email.dispatch_report(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date="2026-07-06",
            alert_type="wbr_weekly",
            entity="all",
            subject="subj",
            body="body",
            apply=False,
        )
        assert result.status == "dry_run"
        assert session.query(AlertDispatch).count() == 0


# ── cross-report alert_key namespace ─────────────────────────────────────


class TestCrossReportKeyNamespace:
    """REQ-WBR-003 / REQ-TXF-004 / REQ-SEL delivery — P3-crosskey2
    regression: ``alert_dispatch``'s unique constraint is
    ``(alert_key, occurrence_date)``, so two DIFFERENT report types
    (WBR/TXF/SEL) that happen to anchor to the SAME occurrence_date are only
    safe from colliding because every report prefixes its own alert_key
    namespace (``wbr:``/``txf:``/``sel:``). This locks that invariant in so
    a future report refactor can't silently drop the prefix and cause one
    report type's catch-up run to dedupe against another report type's
    already-sent row."""

    def test_same_occurrence_date_different_report_types_get_distinct_ledger_rows(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        send_calls: list[object] = []

        def _fake_send(params: object) -> dict[str, str]:
            send_calls.append(params)
            return {"id": "abc"}

        monkeypatch.setattr(_resend.Emails, "send", _fake_send)

        shared_date = "2026-07-06"
        wbr_result = report_email.dispatch_report(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date=shared_date,
            alert_type="wbr_weekly",
            entity="all",
            subject="wbr subj",
            body="wbr body",
            apply=True,
        )
        txf_result = report_email.dispatch_report(
            session,
            alert_key="txf:2026-Q2",
            occurrence_date=shared_date,
            alert_type="tax_forecast",
            entity="all",
            subject="txf subj",
            body="txf body",
            apply=True,
        )
        assert wbr_result.status == "sent"
        assert txf_result.status == "sent"
        assert len(send_calls) == 2

        rows = session.query(AlertDispatch).filter_by(occurrence_date=shared_date).all()
        assert len(rows) == 2
        assert {r.alert_key for r in rows} == {"wbr:2026-W28", "txf:2026-Q2"}

        # Re-dispatching the WBR key must dedupe against ONLY its own key —
        # the TXF row sharing the same occurrence_date must be untouched and
        # no new row created.
        wbr_retry = report_email.dispatch_report(
            session,
            alert_key="wbr:2026-W28",
            occurrence_date=shared_date,
            alert_type="wbr_weekly",
            entity="all",
            subject="wbr subj",
            body="wbr body",
            apply=True,
        )
        assert wbr_retry.status == "skipped"
        assert len(send_calls) == 2  # no additional send call

        rows_after = session.query(AlertDispatch).filter_by(occurrence_date=shared_date).all()
        assert len(rows_after) == 2  # still exactly 2 — no dupe row created
        txf_row = (
            session.query(AlertDispatch)
            .filter_by(alert_key="txf:2026-Q2", occurrence_date=shared_date)
            .one()
        )
        assert txf_row.status == "sent"  # untouched by the WBR retry


# ── build_html_body ───────────────────────────────────────────────────────


class TestBuildHtmlBody:
    def test_escapes_html_special_characters(self) -> None:
        html = report_email.build_html_body("<script>alert(1)</script> & co")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp; co" in html
