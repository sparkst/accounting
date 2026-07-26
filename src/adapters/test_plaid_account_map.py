"""Tests for src/adapters/plaid_account_map.py (fixes P0-001).

REQ-PC-B1/B5 follow-up: the box is the only actor that ever learns a new
plaid_account_id exists post-cutover (at /exchange time for a wealth-scope
Item). Without pushing that mapping to D1, A1/A2 would per-row skip every
balance/holding for that Item forever.
"""

from __future__ import annotations

from typing import Any

from src.adapters._shared.wealth_client import WealthHTTPError
from src.adapters.plaid_account_map import (
    WEALTH_ACCOUNT_MAP_INGEST_SOURCE,
    build_account_map_payload,
    push_account_map,
)


def _account(
    account_id: str = "p_acct_1",
    *,
    name: str | None = "Brokerage",
    official_name: str | None = None,
    mask: str | None = "1234",
    acct_type: str | None = "investment",
    subtype: str | None = "brokerage",
    iso: str | None = "USD",
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "name": name,
        "official_name": official_name,
        "mask": mask,
        "type": acct_type,
        "subtype": subtype,
        "balances": {"current": 100.0, "iso_currency_code": iso},
    }


def test_build_payload_maps_fields() -> None:
    payload = build_account_map_payload([_account()], institution_name="Vanguard")
    assert payload == {
        "mappings": [
            {
                "plaid_account_id": "p_acct_1",
                "institution_name": "Vanguard",
                "account_name": "Brokerage",
                "mask": "1234",
                "plaid_account_type": "investment",
                "plaid_account_subtype": "brokerage",
                "iso_currency_code": "USD",
            }
        ]
    }


def test_build_payload_falls_back_to_official_name() -> None:
    payload = build_account_map_payload(
        [_account(name=None, official_name="Official Brokerage Name")],
        institution_name="Vanguard",
    )
    assert payload["mappings"][0]["account_name"] == "Official Brokerage Name"


def test_build_payload_skips_accounts_without_account_id() -> None:
    payload = build_account_map_payload(
        [{"name": "no id"}, _account()], institution_name="Vanguard"
    )
    assert len(payload["mappings"]) == 1


def test_push_posts_with_correct_slug() -> None:
    calls: list[tuple[dict[str, Any], str]] = []

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        calls.append((payload, source))
        return {"created": 1}

    resp = push_account_map([_account()], institution_name="Vanguard", post=_post)
    assert resp == {"created": 1}
    assert len(calls) == 1
    assert calls[0][1] == WEALTH_ACCOUNT_MAP_INGEST_SOURCE
    assert calls[0][0]["mappings"][0]["plaid_account_id"] == "p_acct_1"


def test_push_with_no_accounts_never_posts() -> None:
    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        raise AssertionError("post should not be called for empty account lists")

    resp = push_account_map([], institution_name="Vanguard", post=_post)
    assert resp is None


def test_push_failure_is_best_effort_returns_none() -> None:
    """A D1 push failure must never raise — the Plaid Link flow already
    succeeded; this is a best-effort follow-up."""

    def _post(payload: dict[str, Any], source: str) -> dict[str, Any]:
        raise WealthHTTPError(500, "d1 down")

    resp = push_account_map([_account()], institution_name="Vanguard", post=_post)
    assert resp is None
