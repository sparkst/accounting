"""Tests for the data-level freshness/invariant sentinel (REQ-SEN-001..008).

Process-level monitoring (exit codes + OnFailure) has repeatedly stayed green
while data went stale or wrong — the sentinel asserts *data* invariants daily:
item sync recency, per-source ingestion recency, register snapshot recency,
scope anomalies (the Schwab/Vanguard mislink signature), register transaction
recency, and report artifact freshness.

Pure check functions take an explicit `now` — no clock reads inside.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.base import Base
from src.models.brokerage import Account
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem
from src.models.transaction import Transaction
from src.monitoring.sentinel import (
    SEV2,
    SEV3,
    Violation,
    build_sentinel_payload,
    check_ingestion_staleness,
    check_item_staleness,
    check_register_snapshot_staleness,
    check_register_tx_staleness,
    check_report_freshness,
    check_scope_anomalies,
    run_sentinel,
)

NOW = datetime(2026, 7, 27, 14, 0, 0)

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.rollback()
    for model in (
        PlaidAccountBalanceSnapshot,
        IngestionLog,
        Transaction,
        Account,
        PlaidItem,
    ):
        s.query(model).delete()
    s.commit()
    s.close()


_ITEM_SEQ = iter(range(1_000_000))
_TXN_SEQ = iter(range(1_000_000))


def _item(
    *,
    institution: str = "Chase",
    scope: str = "register",
    status: str = "active",
    last_sync_at: datetime | None = None,
    last_sync_status: str | None = "ok",
) -> PlaidItem:
    return PlaidItem(
        item_id=f"item-{institution}-{scope}-{next(_ITEM_SEQ)}",
        institution_id=f"ins_{institution.lower()}",
        institution_name=institution,
        access_token_encrypted="enc",
        status=status,
        scope=scope,
        last_sync_at=last_sync_at if last_sync_at is not None else NOW - timedelta(hours=10),
        last_sync_status=last_sync_status,
        # Older than the scope-anomaly onboarding grace window relative to the
        # fixed test NOW (the column default would be the real wall clock).
        created_at=NOW - timedelta(days=3),
    )


def _account(item: PlaidItem, *, number: str = "1234") -> Account:
    return Account(
        broker="chase",
        account_number=number,
        account_name=f"Acct {number}",
        account_type="checking",
        entity="personal",
        plaid_item_id=item.id,
        plaid_account_id=f"plaid-acct-{number}",
    )


def _log(source: str, run_at: datetime, status: str = "success") -> IngestionLog:
    return IngestionLog(source=source, run_at=run_at, status=status)


# ---------------------------------------------------------------------------
# REQ-SEN-002: item staleness
# ---------------------------------------------------------------------------


class TestItemStaleness:
    def test_fresh_ok_item_is_clean(self, session: Session) -> None:
        session.add(_item(last_sync_at=NOW - timedelta(hours=10)))
        session.commit()
        assert check_item_staleness(session, NOW) == []

    def test_stale_item_violates_sev2(self, session: Session) -> None:
        session.add(_item(last_sync_at=NOW - timedelta(hours=30)))
        session.commit()
        violations = check_item_staleness(session, NOW)
        assert len(violations) == 1
        assert violations[0].severity == SEV2
        assert violations[0].check == "item_stale"
        assert "Chase" in violations[0].subject

    def test_error_sync_status_violates_sev2(self, session: Session) -> None:
        session.add(
            _item(last_sync_at=NOW - timedelta(hours=1), last_sync_status="error")
        )
        session.commit()
        violations = check_item_staleness(session, NOW)
        assert len(violations) == 1
        assert violations[0].severity == SEV2

    def test_never_synced_item_violates(self, session: Session) -> None:
        item = _item(last_sync_status=None)
        item.last_sync_at = None
        session.add(item)
        session.commit()
        assert len(check_item_staleness(session, NOW)) == 1

    def test_inactive_items_ignored(self, session: Session) -> None:
        session.add(
            _item(status="disconnected", last_sync_at=NOW - timedelta(days=30))
        )
        session.commit()
        assert check_item_staleness(session, NOW) == []


# ---------------------------------------------------------------------------
# REQ-SEN-003: per-source ingestion recency, expectations derived from items
# ---------------------------------------------------------------------------


def _seed_all_expected_logs(session: Session, when: datetime) -> None:
    for source in (
        "stripe",
        "shopify",
        "wealth_cloud:plaid_balance",
        "plaid_balance:Chase",
        "plaid_tx:Chase",
    ):
        session.add(_log(source, when))


class TestIngestionStaleness:
    def test_all_fresh_is_clean(self, session: Session) -> None:
        session.add(_item(institution="Chase", scope="register"))
        _seed_all_expected_logs(session, NOW - timedelta(hours=9))
        session.commit()
        assert check_ingestion_staleness(session, NOW) == []

    def test_missing_source_violates_sev2(self, session: Session) -> None:
        session.add(_item(institution="Chase", scope="register"))
        _seed_all_expected_logs(session, NOW - timedelta(hours=9))
        session.query(IngestionLog).filter_by(source="stripe").delete()
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert [v.subject for v in violations] == ["stripe"]
        assert violations[0].severity == SEV2

    def test_stale_success_violates(self, session: Session) -> None:
        session.add(_item(institution="Chase", scope="register"))
        _seed_all_expected_logs(session, NOW - timedelta(hours=9))
        session.query(IngestionLog).filter_by(source="shopify").delete()
        session.add(_log("shopify", NOW - timedelta(hours=40)))
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert [v.subject for v in violations] == ["shopify"]

    def test_recent_failure_with_old_success_violates(self, session: Session) -> None:
        """A fresh hard-failure row does not satisfy the recency assertion
        (real IngestionStatus.FAILURE value, not an arbitrary string)."""
        from src.models.enums import IngestionStatus

        session.add(_item(institution="Chase", scope="register"))
        _seed_all_expected_logs(session, NOW - timedelta(hours=9))
        session.query(IngestionLog).filter_by(source="stripe").delete()
        session.add(_log("stripe", NOW - timedelta(hours=40), status="success"))
        session.add(
            _log("stripe", NOW - timedelta(hours=2), IngestionStatus.FAILURE.value)
        )
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert [v.subject for v in violations] == ["stripe"]

    def test_wealth_item_expects_investments_source(self, session: Session) -> None:
        session.add(_item(institution="Vanguard", scope="wealth"))
        _seed_all_expected_logs(session, NOW - timedelta(hours=9))
        session.add(_log("plaid_balance:Vanguard", NOW - timedelta(hours=9)))
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert [v.subject for v in violations] == ["plaid_investments:Vanguard"]
        # plaid_tx is NOT expected for wealth-scope items
        assert all(v.subject != "plaid_tx:Vanguard" for v in violations)


# ---------------------------------------------------------------------------
# REQ-SEN-004: register-mapped accounts have a recent balance snapshot
# ---------------------------------------------------------------------------


class TestRegisterSnapshotStaleness:
    def test_fresh_snapshot_is_clean(self, session: Session) -> None:
        item = _item()
        session.add(item)
        session.flush()
        acct = _account(item)
        session.add(acct)
        session.flush()
        session.add(
            PlaidAccountBalanceSnapshot(
                account_id=acct.id,
                snapshot_date=(NOW - timedelta(days=1)).date(),
                plaid_account_type="depository",
                current_balance=100,
                pulled_at=NOW - timedelta(days=1),
                raw_data={},
            )
        )
        session.commit()
        assert check_register_snapshot_staleness(session, NOW) == []

    def test_missing_snapshot_violates_sev3(self, session: Session) -> None:
        item = _item()
        session.add(item)
        session.flush()
        session.add(_account(item))
        session.commit()
        violations = check_register_snapshot_staleness(session, NOW)
        assert len(violations) == 1
        assert violations[0].severity == SEV3

    def test_stale_snapshot_violates(self, session: Session) -> None:
        item = _item()
        session.add(item)
        session.flush()
        acct = _account(item)
        session.add(acct)
        session.flush()
        session.add(
            PlaidAccountBalanceSnapshot(
                account_id=acct.id,
                snapshot_date=(NOW - timedelta(days=5)).date(),
                plaid_account_type="depository",
                current_balance=100,
                pulled_at=NOW - timedelta(days=5),
                raw_data={},
            )
        )
        session.commit()
        assert len(check_register_snapshot_staleness(session, NOW)) == 1

    def test_wealth_scope_accounts_ignored(self, session: Session) -> None:
        item = _item(scope="wealth")
        session.add(item)
        session.flush()
        session.add(_account(item))
        session.commit()
        assert check_register_snapshot_staleness(session, NOW) == []


# ---------------------------------------------------------------------------
# REQ-SEN-005: scope anomalies (the mislink signature)
# ---------------------------------------------------------------------------


class TestScopeAnomalies:
    def test_register_item_with_zero_mapped_accounts_is_sev2(
        self, session: Session
    ) -> None:
        session.add(_item(institution="Charles Schwab", scope="register"))
        session.commit()
        violations = check_scope_anomalies(session, NOW)
        assert len(violations) == 1
        assert violations[0].severity == SEV2
        assert violations[0].check == "scope_anomaly"
        assert "Charles Schwab" in violations[0].subject

    def test_register_item_with_mapped_account_is_clean(
        self, session: Session
    ) -> None:
        item = _item()
        session.add(item)
        session.flush()
        session.add(_account(item))
        session.commit()
        assert check_scope_anomalies(session, NOW) == []

    def test_wealth_item_with_mapped_register_account_is_sev3(
        self, session: Session
    ) -> None:
        item = _item(institution="Vanguard", scope="wealth")
        session.add(item)
        session.flush()
        session.add(_account(item))
        session.commit()
        violations = check_scope_anomalies(session, NOW)
        assert len(violations) == 1
        assert violations[0].severity == SEV3

    def test_inactive_items_ignored(self, session: Session) -> None:
        session.add(_item(status="abandoned", scope="register"))
        session.commit()
        assert check_scope_anomalies(session, NOW) == []

    def test_unknown_scope_violates_sev3(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P3-scp: an unrecognized scope emits a defensive violation. The DB
        CHECK constraint makes this unreachable through SQL today, so the
        defensive branch is exercised with an in-memory item."""
        from src.monitoring import sentinel as mod

        item = _item(institution="Unknown Broker", scope="register")
        item.scope = "invalid"
        monkeypatch.setattr(mod, "_active_items", lambda s: [item])
        violations = check_scope_anomalies(session, NOW)
        assert len(violations) == 1
        assert violations[0].severity == SEV3
        assert violations[0].check == "scope_anomaly"
        assert "unrecognized scope" in violations[0].detail


# ---------------------------------------------------------------------------
# REQ-SEN-006: register plaid transactions keep flowing
# ---------------------------------------------------------------------------


def _txn(*, date_s: str, source: str = "plaid", status: str = "auto_classified") -> Transaction:
    return Transaction(
        source=source,
        source_hash=f"h-{date_s}-{next(_TXN_SEQ)}",
        date=date_s,
        description="t",
        amount=-1,
        entity="personal",
        status=status,
        raw_data={},
    )


class TestRegisterTxStaleness:
    def test_recent_plaid_txn_is_clean(self, session: Session) -> None:
        session.add(_item())  # an active register item exists
        session.add(_txn(date_s=(NOW - timedelta(days=3)).date().isoformat()))
        session.commit()
        assert check_register_tx_staleness(session, NOW) == []

    def test_old_plaid_txn_violates_sev3(self, session: Session) -> None:
        session.add(_item())
        session.add(_txn(date_s=(NOW - timedelta(days=12)).date().isoformat()))
        session.commit()
        violations = check_register_tx_staleness(session, NOW)
        assert len(violations) == 1
        assert violations[0].severity == SEV3

    def test_rejected_rows_do_not_count_as_fresh(self, session: Session) -> None:
        session.add(_item())
        session.add(_txn(date_s=(NOW - timedelta(days=12)).date().isoformat()))
        session.add(
            _txn(date_s=(NOW - timedelta(days=1)).date().isoformat(), status="rejected")
        )
        session.commit()
        assert len(check_register_tx_staleness(session, NOW)) == 1

    def test_no_register_items_no_expectation(self, session: Session) -> None:
        session.add(_item(scope="wealth", institution="Vanguard"))
        session.commit()
        assert check_register_tx_staleness(session, NOW) == []


# ---------------------------------------------------------------------------
# REQ-SEN-007: report artifact freshness
# ---------------------------------------------------------------------------


class TestReportFreshness:
    def test_missing_file_violates_sev3(self, tmp_path: Path) -> None:
        violations = check_report_freshness(tmp_path / "weekly-pl-latest.txt", NOW)
        assert len(violations) == 1
        assert violations[0].severity == SEV3

    def test_fresh_file_is_clean(self, tmp_path: Path) -> None:
        f = tmp_path / "weekly-pl-latest.txt"
        f.write_text("report")
        import os

        mtime = (NOW - timedelta(days=2)).timestamp()
        os.utime(f, (mtime, mtime))
        assert check_report_freshness(f, NOW) == []

    def test_old_file_violates(self, tmp_path: Path) -> None:
        f = tmp_path / "weekly-pl-latest.txt"
        f.write_text("report")
        import os

        mtime = (NOW - timedelta(days=9)).timestamp()
        os.utime(f, (mtime, mtime))
        assert len(check_report_freshness(f, NOW)) == 1


# ---------------------------------------------------------------------------
# REQ-SEN-001/008: composition + webhook payload aggregation
# ---------------------------------------------------------------------------


class TestRunSentinel:
    def test_clean_db_and_fresh_report_yield_no_violations(
        self, session: Session, tmp_path: Path
    ) -> None:
        item = _item()
        session.add(item)
        session.flush()
        acct = _account(item)
        session.add(acct)
        session.flush()
        session.add(
            PlaidAccountBalanceSnapshot(
                account_id=acct.id,
                snapshot_date=(NOW - timedelta(days=1)).date(),
                plaid_account_type="depository",
                current_balance=100,
                pulled_at=NOW - timedelta(days=1),
                raw_data={},
            )
        )
        session.add(_txn(date_s=(NOW - timedelta(days=1)).date().isoformat()))
        _seed_all_expected_logs(session, NOW - timedelta(hours=9))
        session.commit()
        f = tmp_path / "weekly-pl-latest.txt"
        f.write_text("report")
        import os

        mtime = (NOW - timedelta(days=1)).timestamp()
        os.utime(f, (mtime, mtime))

        assert run_sentinel(session, NOW, report_path=f) == []

    def test_violations_from_multiple_checks_aggregate(
        self, session: Session, tmp_path: Path
    ) -> None:
        session.add(_item(last_sync_at=NOW - timedelta(hours=40)))
        session.commit()
        violations = run_sentinel(
            session, NOW, report_path=tmp_path / "missing.txt"
        )
        checks = {v.check for v in violations}
        assert "item_stale" in checks
        assert "report_stale" in checks


class TestPayload:
    def test_no_violations_returns_none(self) -> None:
        assert build_sentinel_payload([], NOW) is None

    def test_payload_type_is_max_severity(self) -> None:
        violations = [
            Violation("report_stale", SEV3, "weekly-pl", "9 days old"),
            Violation("item_stale", SEV2, "Chase", "no sync 40h"),
        ]
        payload = build_sentinel_payload(violations, NOW)
        assert payload is not None
        assert payload["type"] == SEV2
        assert "Chase" in (payload["message"] or "")
        assert "weekly-pl" in (payload["message"] or "")
        assert payload["alert_key"] == "sentinel:2026-07-27"


# ---------------------------------------------------------------------------
# REQ-SEN-008: dispatch — post when violations exist, exit semantics
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_clean_run_posts_nothing(
        self, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.monitoring import sentinel as mod

        posted: list[dict] = []
        monkeypatch.setattr(
            mod,
            "post_payload",
            lambda payload, *, key, apply: posted.append(payload) or _wr("sent"),
        )
        for source in ("stripe", "shopify", "wealth_cloud:plaid_balance"):
            session.add(_log(source, NOW - timedelta(hours=9)))
        session.commit()
        f = tmp_path / "weekly-pl-latest.txt"
        f.write_text("x")
        import os

        mtime = (NOW - timedelta(days=1)).timestamp()
        os.utime(f, (mtime, mtime))
        violations, result = mod.dispatch_sentinel(
            session, NOW, report_path=f, apply=True
        )
        assert violations == []
        assert result is None
        assert posted == []

    def test_violations_posted_once_with_apply(
        self, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.monitoring import sentinel as mod

        posted: list[dict] = []
        monkeypatch.setattr(
            mod,
            "post_payload",
            lambda payload, *, key, apply: posted.append(payload) or _wr("sent"),
        )
        session.add(_item(last_sync_at=NOW - timedelta(hours=40)))
        session.commit()
        violations, result = mod.dispatch_sentinel(
            session, NOW, report_path=tmp_path / "missing.txt", apply=True
        )
        assert len(violations) >= 2
        assert len(posted) == 1
        assert result is not None and result.status == "sent"

    def test_dry_run_does_not_post(
        self, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.monitoring import sentinel as mod

        posted: list[dict] = []
        monkeypatch.setattr(
            mod,
            "post_payload",
            lambda payload, *, key, apply: posted.append((payload, apply)) or _wr("dry_run"),
        )
        session.add(_item(last_sync_at=NOW - timedelta(hours=40)))
        session.commit()
        violations, result = mod.dispatch_sentinel(
            session, NOW, report_path=tmp_path / "missing.txt", apply=False
        )
        assert violations
        # post_payload is still called (it handles dry-run itself) with apply=False
        assert posted and posted[0][1] is False


def _wr(status: str):
    from src.alerts.webhook import WebhookResult

    return WebhookResult(status, 200 if status == "sent" else None, None)


# ---------------------------------------------------------------------------
# Helper function tests: _as_date (P3-n4c)
# ---------------------------------------------------------------------------


class TestAsDate:
    def test_as_date_with_date_object(self) -> None:
        from src.monitoring.sentinel import _as_date

        d = datetime(2026, 7, 27).date()
        assert _as_date(d) == d

    def test_as_date_with_iso_date_string(self) -> None:
        from src.monitoring.sentinel import _as_date

        d = _as_date("2026-07-27")
        assert d == datetime(2026, 7, 27).date()

    def test_as_date_with_iso_datetime_string(self) -> None:
        from src.monitoring.sentinel import _as_date

        d = _as_date("2026-07-27T14:00:00")
        assert d == datetime(2026, 7, 27).date()


# ---------------------------------------------------------------------------
# qreview P1 fixes (2026-07-27): TZ discipline, dup-institution ambiguity,
# conditional wealth_cloud expectation, CLI exit-code contract
# ---------------------------------------------------------------------------


class TestUtcDiscipline:
    def test_report_mtime_compared_in_utc(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """P1-001: st_mtime must be interpreted as UTC-naive to match `now`
        (NOW is UTC-naive by repo convention). Under TZ=US Pacific a local-time
        `fromtimestamp` reads the same instant 7-8h older, so a file that is
        FRESH in UTC terms (7d20h < 8d) would falsely violate."""
        import os
        import time

        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        try:
            from datetime import UTC

            f = tmp_path / "weekly-pl-latest.txt"
            f.write_text("x")
            mtime = (NOW.replace(tzinfo=UTC) - timedelta(days=7, hours=20)).timestamp()
            os.utime(f, (mtime, mtime))
            assert check_report_freshness(f, NOW) == []
        finally:
            monkeypatch.delenv("TZ", raising=False)
            time.tzset()

    def test_cli_now_is_utc_naive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P1-001: main() must reference UTC-naive now (repo convention for
        every DB timestamp it compares against), not system-local now."""
        import time
        from datetime import UTC

        import scripts.freshness_sentinel as cli

        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        try:
            monkeypatch.setattr(cli, "init_db", lambda: None)

            class _S:
                def close(self) -> None: ...

            monkeypatch.setattr(cli, "SessionLocal", lambda: _S())
            seen: list[datetime] = []

            def _capture(session, now, **kwargs):  # type: ignore[no-untyped-def]
                seen.append(now)
                return [], None

            monkeypatch.setattr(cli, "dispatch_sentinel", _capture)
            assert cli.main([]) == 0
            utc_now = datetime.now(UTC).replace(tzinfo=None)
            assert abs((seen[0] - utc_now).total_seconds()) < 300
        finally:
            monkeypatch.delenv("TZ", raising=False)
            time.tzset()


class TestDuplicateInstitutionAmbiguity:
    def test_stale_item_behind_fresh_shared_source_emits_ambiguity(
        self, session: Session
    ) -> None:
        """P1-002: two active Vanguard items share ingestion_log source keys, so
        one failing item is masked by the other's success. The marker fires
        exactly when masking is possible — a name-sharing item is stale while
        the shared source key looks fresh."""
        session.add(_item(institution="Vanguard", scope="wealth"))
        session.add(
            _item(
                institution="Vanguard",
                scope="wealth",
                last_sync_at=NOW - timedelta(hours=40),
            )
        )
        session.add(_log("plaid_balance:Vanguard", NOW - timedelta(hours=9)))
        session.add(_log("plaid_investments:Vanguard", NOW - timedelta(hours=9)))
        for source in ("stripe", "shopify", "wealth_cloud:plaid_balance"):
            session.add(_log(source, NOW - timedelta(hours=9)))
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        ambiguous = [v for v in violations if v.check == "ingest_source_ambiguous"]
        assert len(ambiguous) == 1
        assert ambiguous[0].severity == SEV3
        assert "Vanguard" in ambiguous[0].subject

    def test_all_fresh_duplicate_institutions_stay_quiet(
        self, session: Session
    ) -> None:
        """Two healthy items sharing a name is production's PERMANENT state
        (Chase register+wealth; Travis + Amy Vanguard logins) — a daily
        violation for it would be pure alert fatigue (the incident-5 lesson)."""
        session.add(_item(institution="Vanguard", scope="wealth"))
        session.add(_item(institution="Vanguard", scope="wealth"))
        session.add(_log("plaid_balance:Vanguard", NOW - timedelta(hours=9)))
        session.add(_log("plaid_investments:Vanguard", NOW - timedelta(hours=9)))
        for source in ("stripe", "shopify", "wealth_cloud:plaid_balance"):
            session.add(_log(source, NOW - timedelta(hours=9)))
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert all(v.check != "ingest_source_ambiguous" for v in violations)

    def test_single_item_per_institution_no_ambiguity(self, session: Session) -> None:
        session.add(_item(institution="Chase", scope="register"))
        _seed_all_expected_logs(session, NOW - timedelta(hours=9))
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert all(v.check != "ingest_source_ambiguous" for v in violations)


class TestConditionalWealthCloudExpectation:
    def test_no_wealth_items_no_wealth_cloud_expectation(self, session: Session) -> None:
        """P1-003: the producer only writes wealth_cloud:plaid_balance when
        wealth items exist — expecting it unconditionally would cry a false
        sev2 daily forever after the last wealth item is disconnected."""
        session.add(_item(institution="Chase", scope="register"))
        for source in ("stripe", "shopify", "plaid_balance:Chase", "plaid_tx:Chase"):
            session.add(_log(source, NOW - timedelta(hours=9)))
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert all(v.subject != "wealth_cloud:plaid_balance" for v in violations)

    def test_wealth_item_present_expects_wealth_cloud(self, session: Session) -> None:
        session.add(_item(institution="Vanguard", scope="wealth"))
        for source in ("stripe", "shopify", "plaid_balance:Vanguard", "plaid_investments:Vanguard"):
            session.add(_log(source, NOW - timedelta(hours=9)))
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert any(v.subject == "wealth_cloud:plaid_balance" for v in violations)


class TestCliExitContract:
    """P1-004: REQ-SEN-008's exit-code contract, tested against main()."""

    def _run_main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        argv: list[str],
        violations: list[Violation],
        webhook_status: str | None,
    ) -> int:
        import scripts.freshness_sentinel as cli

        monkeypatch.setattr(cli, "init_db", lambda: None)

        class _S:
            def close(self) -> None: ...

        monkeypatch.setattr(cli, "SessionLocal", lambda: _S())
        result = None if webhook_status is None else _wr(webhook_status)
        monkeypatch.setattr(
            cli, "dispatch_sentinel", lambda *a, **k: (violations, result)
        )
        return cli.main(argv)

    def test_clean_run_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._run_main(monkeypatch, argv=[], violations=[], webhook_status=None) == 0

    def test_violations_with_sent_webhook_exit_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        v = [Violation("item_stale", SEV2, "Chase", "40h")]
        rc = self._run_main(
            monkeypatch, argv=["--apply"], violations=v, webhook_status="sent"
        )
        assert rc == 0

    def test_violations_with_failed_webhook_exit_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        v = [Violation("item_stale", SEV2, "Chase", "40h")]
        rc = self._run_main(
            monkeypatch, argv=["--apply"], violations=v, webhook_status="failed"
        )
        assert rc == 1

    def test_dry_run_violations_exit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        v = [Violation("report_stale", SEV3, "weekly-pl", "9d")]
        rc = self._run_main(monkeypatch, argv=[], violations=v, webhook_status="dry_run")
        assert rc == 0

    def test_infra_failure_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.freshness_sentinel as cli

        def _boom() -> None:
            raise RuntimeError("db unreachable")

        monkeypatch.setattr(cli, "init_db", _boom)
        assert cli.main([]) == 1


# ---------------------------------------------------------------------------
# Round-2 review fixes: partial_failure semantics (P1-a1f/P2-g7b), per-check
# isolation (P2-c3d), digest cap (P2-e5f), heartbeat (P2-f6a), order (P2-d4e)
# ---------------------------------------------------------------------------


class TestPartialFailureSemantics:
    def _seed_static(self, session: Session) -> None:
        session.add(_item(institution="Chase", scope="register"))
        for source in ("plaid_balance:Chase", "plaid_tx:Chase", "shopify"):
            session.add(_log(source, NOW - timedelta(hours=9)))

    def test_recent_partial_failure_satisfies_recency_but_flags_degraded(
        self, session: Session
    ) -> None:
        """P1-a1f: per-record isolation means one bad row downgrades a healthy
        run to partial_failure — that must NOT read as 'source dead' (sev2
        daily forever), but as a sev3 degraded marker."""
        from src.models.enums import IngestionStatus

        self._seed_static(session)
        session.add(
            _log("stripe", NOW - timedelta(hours=2), IngestionStatus.PARTIAL_FAILURE.value)
        )
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert all(
            not (v.check == "ingest_stale" and v.subject == "stripe") for v in violations
        )
        degraded = [v for v in violations if v.check == "ingest_degraded"]
        assert [v.subject for v in degraded] == ["stripe"]
        assert degraded[0].severity == SEV3

    def test_success_newer_than_partial_failure_is_clean(self, session: Session) -> None:
        from src.models.enums import IngestionStatus

        self._seed_static(session)
        session.add(
            _log("stripe", NOW - timedelta(hours=9), IngestionStatus.PARTIAL_FAILURE.value)
        )
        session.add(_log("stripe", NOW - timedelta(hours=2), IngestionStatus.SUCCESS.value))
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert all(v.subject != "stripe" for v in violations)

    def test_recent_hard_failure_only_violates_sev2(self, session: Session) -> None:
        from src.models.enums import IngestionStatus

        self._seed_static(session)
        session.add(
            _log("stripe", NOW - timedelta(hours=2), IngestionStatus.FAILURE.value)
        )
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        stale = [v for v in violations if v.check == "ingest_stale" and v.subject == "stripe"]
        assert len(stale) == 1
        assert stale[0].severity == SEV2


class TestRunSentinelResilience:
    def test_one_raising_check_does_not_suppress_others(
        self, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2-c3d: per-check isolation (the house per-record pattern) — a
        broken check becomes a violation, not a dead sentinel."""
        from src.monitoring import sentinel as mod

        def _boom(session: Session, now: datetime, **kw: object) -> list[Violation]:
            raise RuntimeError("schema drift")

        monkeypatch.setattr(mod, "check_item_staleness", _boom)
        violations = run_sentinel(session, NOW, report_path=tmp_path / "missing.txt")
        checks = {v.check for v in violations}
        assert "check_failed" in checks
        assert "report_stale" in checks  # the other checks still ran

    def test_worst_severity_sorts_first(self, session: Session, tmp_path: Path) -> None:
        """P2-d4e: REQ-SEN-001's worst-first ordering, asserted end-to-end."""
        session.add(_item(last_sync_at=NOW - timedelta(hours=40)))  # sev2
        session.commit()
        violations = run_sentinel(session, NOW, report_path=tmp_path / "missing.txt")
        severities = [v.severity for v in violations]
        assert SEV2 in severities and SEV3 in severities
        assert severities == sorted(severities, key=lambda s: {SEV2: 0, SEV3: 1}[s])


class TestPayloadCap:
    def test_digest_caps_lines_with_footer(self) -> None:
        """P2-e5f: Telegram hard-limits messages at 4096 chars — an unbounded
        digest would drop the alert for exactly the worst incidents."""
        violations = [
            Violation("item_stale", SEV2, f"Institution {i}", "x" * 80)
            for i in range(60)
        ]
        payload = build_sentinel_payload(violations, NOW)
        assert payload is not None
        message = payload["message"] or ""
        assert len(message) < 3600
        assert "+35 more violation(s)" in message


class TestHeartbeat:
    def test_apply_run_writes_heartbeat_row(
        self, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2-f6a: the sentinel records its own run in ingestion_log so other
        surfaces (delivery-health, monthly close, its own next run) can see the
        watchdog went quiet."""
        from src.monitoring import sentinel as mod

        monkeypatch.setattr(
            mod, "post_payload", lambda payload, *, key, apply: _wr("sent")
        )
        for source in ("stripe", "shopify"):
            session.add(_log(source, NOW - timedelta(hours=9)))
        session.commit()
        f = tmp_path / "weekly-pl-latest.txt"
        f.write_text("x")
        import os

        mtime = (NOW - timedelta(days=1)).timestamp()
        os.utime(f, (mtime, mtime))
        mod.dispatch_sentinel(session, NOW, report_path=f, apply=True)
        rows = session.query(IngestionLog).filter_by(source="freshness_sentinel").all()
        assert len(rows) == 1
        assert rows[0].status == "success"

    def test_dry_run_writes_no_heartbeat(
        self, session: Session, tmp_path: Path
    ) -> None:
        from src.monitoring import sentinel as mod

        mod.dispatch_sentinel(
            session, NOW, report_path=tmp_path / "missing.txt", apply=False
        )
        assert session.query(IngestionLog).filter_by(source="freshness_sentinel").count() == 0

    def test_prior_heartbeat_makes_sentinel_self_expected(
        self, session: Session
    ) -> None:
        """Once a heartbeat exists, a stale one violates — the sentinel watches
        itself. No heartbeat history → no expectation (fresh installs clean)."""
        session.add(_log("freshness_sentinel", NOW - timedelta(hours=40)))
        for source in ("stripe", "shopify"):
            session.add(_log(source, NOW - timedelta(hours=9)))
        session.commit()
        violations = check_ingestion_staleness(session, NOW)
        assert any(v.subject == "freshness_sentinel" for v in violations)
