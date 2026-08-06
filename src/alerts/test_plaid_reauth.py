"""Tests for src/alerts/plaid_reauth.py (REQ-FIX-ALR-009)."""

from __future__ import annotations

import unittest.mock as mock
from pathlib import Path

import pytest

from src.alerts import plaid_reauth as pr
from src.alerts.plaid_reauth import ItemFailure, route_item_failures
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
        routing = route_item_failures([PENFED, DOWN, D1], [], apply=True)
    assert routing.reauth == [PENFED]
    assert routing.infra == [DOWN, D1]


def test_unexpected_and_none_error_codes_are_infra() -> None:
    with mock.patch.object(pr, "post_payload", return_value=_sent()):
        routing = route_item_failures(
            [ItemFailure("i", "X", "UNEXPECTED"), ItemFailure("j", "Y", None)],
            [],
            apply=True,
        )
    assert routing.reauth == []
    assert len(routing.infra) == 2


def test_alert_posted_once_per_error_state(tmp_path: Path) -> None:
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        first = route_item_failures([PENFED], [], apply=True)
        second = route_item_failures([PENFED], [], apply=True)
    assert first.alerts_sent == 1
    assert second.alerts_sent == 0
    assert post.call_count == 1  # sentinel dedup across runs (and across scripts)


def test_new_error_state_realerts(tmp_path: Path) -> None:
    expired = ItemFailure("item-1", "PenFed Credit Union", "PENDING_EXPIRATION")
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        route_item_failures([PENFED], [], apply=True)
        route_item_failures([expired], [], apply=True)
    assert post.call_count == 2


def test_recovery_clears_sentinels_so_rebreak_realerts(tmp_path: Path) -> None:
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        route_item_failures([PENFED], [], apply=True)
        route_item_failures([], ["item-1"], apply=True)  # recovered
        route_item_failures([PENFED], [], apply=True)  # broke again
    assert post.call_count == 2
    assert list(tmp_path.glob("plaid-reauth-item-1-*")) != []


def test_failed_post_leaves_no_sentinel_and_retries(tmp_path: Path) -> None:
    with mock.patch.object(pr, "post_payload", return_value=_failed()):
        routing = route_item_failures([PENFED], [], apply=True)
    assert routing.alerts_sent == 0
    assert list(tmp_path.glob("plaid-reauth-*")) == []
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        route_item_failures([PENFED], [], apply=True)
    assert post.call_count == 1


def test_dry_run_never_posts_or_touches_sentinels(tmp_path: Path) -> None:
    with mock.patch.object(pr, "post_payload", return_value=WebhookResult("dry_run", None, None)) as post:
        routing = route_item_failures([PENFED, DOWN], ["item-9"], apply=False)
    assert routing.reauth == [PENFED]
    assert routing.infra == [DOWN]
    assert routing.alerts_sent == 0
    # post_payload is called with apply=False (its own dry-run path) — no sentinel.
    assert post.call_args.kwargs["apply"] is False
    assert list(tmp_path.iterdir()) == []


def test_payload_contract_sev3_with_reconnect_link() -> None:
    with mock.patch.object(pr, "post_payload", return_value=_sent()) as post:
        route_item_failures([PENFED], [], apply=True)
    payload = post.call_args.args[0]
    assert payload["type"] == "sev3"
    assert "PenFed Credit Union" in payload["title"]
    assert pr.RECONNECT_URL in payload["message"]
    assert "ITEM_LOGIN_REQUIRED" in payload["message"]
    assert payload["alert_key"] == "plaid-reauth:item-1:ITEM_LOGIN_REQUIRED"
