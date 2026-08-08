"""Tests for src/alerts/plaid_reauth.py (REQ-FIX-ALR-009)."""

from __future__ import annotations

import unittest.mock as mock
from pathlib import Path

import pytest

from src.adapters.plaid_client import TERMINAL_ERROR_CODES
from src.alerts import plaid_reauth as pr
from src.alerts.plaid_reauth import ItemFailure, route_batch, route_item_failures
from src.alerts.webhook import WebhookResult


@pytest.fixture(autouse=True)
def _sentinel_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ALERT_SENTINEL_DIR", str(tmp_path))
    return tmp_path


def _sent() -> WebhookResult:
    return WebhookResult("sent", 200, None)


def _failed() -> WebhookResult:
    return WebhookResult("failed", None, "network error")


PENFED = ItemFailure("item-1", "PenFed Credit Union", "ITEM_LOGIN_REQUIRED")
DOWN = ItemFailure("item-2", "Chase", "INSTITUTION_DOWN")
D1 = ItemFailure("item-3", "Vanguard", "D1_PUSH:WealthHTTPError")


def test_partitions_reauth_vs_infra() -> None:
    with mock.patch.object(pr, "post_payload", return_value=_sent()):
        routing = route_item_failures([PENFED, DOWN, D1], [], apply=True, source="balance")
    assert routing.reauth == [PENFED]
    assert routing.infra == [DOWN, D1]
    assert routing.exit_failures  # infra present


def test_reauth_codes_extend_plaid_client_canon() -> None:
    """REAUTH_ERROR_CODES builds on plaid_client.TERMINAL_ERROR_CODES so a
    new terminal code added there propagates here (no divergent second set)."""
    assert TERMINAL_ERROR_CODES <= pr.REAUTH_ERROR_CODES
    assert pr.is_reauth("INVALID_CREDENTIALS")
    assert pr.is_reauth("ITEM_NOT_FOUND")
    assert pr.is_reauth("ADDITIONAL_CONSENT_REQUIRED")
    # 2026-08-08 live: consent-shaped sibling — reaches routing only when an
    # item with delivered-holdings history regressed (re-link, not infra).
    assert pr.is_reauth("PRODUCTS_NOT_SUPPORTED")


def test_unexpected_and_none_error_codes_are_infra() -> None:
    with mock.patch.object(pr, "post_payload", return_value=_sent()):
        routing = route_item_failures(
            [ItemFailure("i", "X", "UNEXPECTED"), ItemFailure("j", "Y", None)],
            [],
            apply=True,
            source="balance",
        )
    assert routing.reauth == []
    assert len(routing.infra) == 2


def test_alert_posted_once_per_error_state_across_sources(tmp_path: Path) -> None:
    """Cross-source dedup: balance (04:00) posts; investments (04:20) and
    transactions (05:00) see the (item, code) state already alerted."""
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        first = route_item_failures([PENFED], [], apply=True, source="balance")
        second = route_item_failures([PENFED], [], apply=True, source="investments")
        third = route_item_failures([PENFED], [], apply=True, source="transactions")
    assert first.alerts_sent == 1
    assert second.alerts_sent == 0
    assert third.alerts_sent == 0
    assert post.call_count == 1


def test_product_scoped_recovery_does_not_clear_other_sources(tmp_path: Path) -> None:
    """The review's daily-re-alert scenario: an Item is consent-broken for
    investments only. The balance sync seeing it 'ok' must NOT clear the
    investments-owned sentinel — otherwise investments re-alerts every day."""
    consent = ItemFailure("item-9", "Schwab", "ADDITIONAL_CONSENT_REQUIRED")
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        route_item_failures([consent], [], apply=True, source="investments")  # day 1: alert
        # day 2, 04:00 — balance sees the Item clean:
        route_item_failures([], ["item-9"], apply=True, source="balance")
        # day 2, 04:20 — investments still broken; must NOT re-post:
        again = route_item_failures([consent], [], apply=True, source="investments")
    assert post.call_count == 1
    assert again.alerts_sent == 0


def test_own_source_recovery_clears_and_rebreak_realerts(tmp_path: Path) -> None:
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        route_item_failures([PENFED], [], apply=True, source="balance")
        route_item_failures([], ["item-1"], apply=True, source="balance")  # recovered
        route_item_failures([PENFED], [], apply=True, source="balance")  # broke again
    assert post.call_count == 2


def test_code_transition_realerts_and_drops_stale_sentinel(tmp_path: Path) -> None:
    """PENDING_EXPIRATION → ITEM_LOGIN_REQUIRED must re-alert and must not
    leave the old code's sentinel behind."""
    expiring = ItemFailure("item-1", "PenFed Credit Union", "PENDING_EXPIRATION")
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        route_item_failures([expiring], [], apply=True, source="balance")
        route_item_failures([PENFED], [], apply=True, source="balance")
    assert post.call_count == 2
    names = [p.name for p in tmp_path.iterdir()]
    assert len(names) == 1
    assert "ITEM_LOGIN_REQUIRED" in names[0]
    assert not any("PENDING_EXPIRATION" in n for n in names)


def test_failed_post_marks_post_failed_and_retries(tmp_path: Path) -> None:
    """A failed POST must surface (exit_failures True → unit exits non-zero →
    OnFailure pages) and leave no sentinel so the next run retries."""
    with mock.patch.object(pr, "post_payload", return_value=_failed()):
        routing = route_item_failures([PENFED], [], apply=True, source="balance")
    assert routing.alerts_sent == 0
    assert routing.post_failed == [PENFED]
    assert routing.exit_failures
    assert list(tmp_path.glob("plaid-reauth-*")) == []
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        retry = route_item_failures([PENFED], [], apply=True, source="balance")
    assert post.call_count == 1
    assert retry.post_failed == []


def test_dry_run_never_posts_or_touches_sentinels(tmp_path: Path) -> None:
    with mock.patch.object(
        pr, "post_payload", return_value=WebhookResult("dry_run", None, None)
    ) as post:
        routing = route_item_failures(
            [PENFED, DOWN], ["item-9"], apply=False, source="balance"
        )
    assert routing.reauth == [PENFED]
    assert routing.infra == [DOWN]
    assert routing.alerts_sent == 0
    assert post.call_args.kwargs["apply"] is False
    assert list(tmp_path.iterdir()) == []


def test_payload_contract_sev3_with_reconnect_link() -> None:
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        route_item_failures([PENFED], [], apply=True, source="balance")
    payload = post.call_args.args[0]
    assert payload["type"] == "sev3"
    assert "PenFed Credit Union" in payload["title"]
    assert pr.RECONNECT_URL in payload["message"]
    assert "ITEM_LOGIN_REQUIRED" in payload["message"]
    assert payload["alert_key"] == "plaid-reauth:item-1:ITEM_LOGIN_REQUIRED"


# ── route_batch: the shared CLI entry point ──────────────────────────────────


def _result(status: str, *, item_id: str = "item-1", code: str | None = None) -> mock.Mock:
    return mock.Mock(
        item_id=item_id, institution_name="PenFed Credit Union",
        status=status, error_code=code,
    )


def test_route_batch_partitions_by_clean_statuses() -> None:
    items = [
        _result("ok", item_id="a"),
        _result("skipped_invalid_product", item_id="b", code="ADDITIONAL_CONSENT_REQUIRED"),
        _result("error", item_id="c", code="ITEM_LOGIN_REQUIRED"),
    ]
    with mock.patch.object(pr, "post_payload", return_value=_sent()):
        routing = route_batch(
            items,
            apply=True,
            source="investments",
            clean_statuses=("ok", "skipped_invalid_product"),
        )
    assert [f.item_id for f in routing.reauth] == ["c"]
    assert routing.infra == []
    assert not routing.exit_failures


def test_route_batch_default_clean_is_ok_only() -> None:
    items = [_result("error", code="UNEXPECTED")]
    with mock.patch.object(pr, "post_payload", return_value=_sent()):
        routing = route_batch(items, apply=True, source="balance")
    assert routing.infra and routing.exit_failures
