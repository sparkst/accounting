"""Tests for scripts/adapter_sync.py.

REQ-ID: REQ-FIX-ING-020  The scheduled Stripe/Shopify sync turns the ingest
                         endpoint's HTTP-200-with-embedded-errors response into
                         a process exit code, so systemd's OnFailure= alert
                         fires on a real failure instead of the run silently
                         "succeeding" — the failure shape that hid a six-week
                         ingestion gap. Known-benign adapter errors are
                         tolerated but always logged; everything else fails.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from scripts.adapter_sync import (
    BENIGN_ERROR_PATTERNS,
    SUPPORTED_SOURCES,
    SyncError,
    is_benign,
    main,
    run_sync,
    summarize,
)

SHOPIFY_PAYOUT_403 = (
    "[shopify] Shopify payouts skipped: 403 "
    "(read_shopify_payments_payouts scope not approved). Orders ingested normally."
)


def _body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ingested_count": 36,
        "classified_count": 25,
        "needs_review_count": 6,
        "adapter_results": [
            {
                "source": "stripe",
                "records_processed": 175,
                "records_created": 36,
                "records_skipped": 259,
                "records_failed": 0,
            }
        ],
        "warnings": [],
        "errors": [],
    }
    payload.update(overrides)
    return payload


def _client(
    status_code: int = 200,
    json_body: dict[str, Any] | None = None,
    text: str | None = None,
    raises: Exception | None = None,
) -> httpx.Client:
    """An httpx.Client whose transport returns a canned response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if raises is not None:
            raise raises
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=json_body if json_body is not None else _body())

    return httpx.Client(transport=httpx.MockTransport(handler))


# ── success path ─────────────────────────────────────────────────────────────


def test_clean_run_is_ok() -> None:
    outcome = run_sync("stripe", api_key="k", client=_client())
    assert outcome.ok
    assert outcome.ingested == 36
    assert outcome.records_failed == 0


def test_outcome_reports_counts_from_body() -> None:
    outcome = run_sync("stripe", api_key="k", client=_client())
    assert (outcome.classified, outcome.needs_review) == (25, 6)


# ── the core defect: HTTP 200 hiding a failure ───────────────────────────────


def test_records_failed_makes_the_run_fail_despite_http_200() -> None:
    body = _body(
        adapter_results=[
            {"source": "stripe", "records_processed": 10, "records_failed": 3}
        ]
    )
    outcome = run_sync("stripe", api_key="k", client=_client(json_body=body))
    assert not outcome.ok
    assert outcome.records_failed == 3


def test_fatal_error_makes_the_run_fail_despite_http_200() -> None:
    body = _body(errors=["[stripe] Could not authenticate: invalid api key"])
    outcome = run_sync("stripe", api_key="k", client=_client(json_body=body))
    assert not outcome.ok
    assert len(outcome.fatal_errors) == 1


def test_records_failed_summed_across_adapters() -> None:
    body = _body(
        adapter_results=[
            {"source": "stripe", "records_failed": 0},
            {"source": "stripe", "records_failed": 2},
        ]
    )
    outcome = run_sync("stripe", api_key="k", client=_client(json_body=body))
    assert outcome.records_failed == 2
    assert not outcome.ok


# ── benign-error allowlist ───────────────────────────────────────────────────


def test_shopify_payout_scope_error_is_benign() -> None:
    assert is_benign(SHOPIFY_PAYOUT_403)


def test_benign_error_alone_does_not_fail_the_run() -> None:
    body = _body(errors=[SHOPIFY_PAYOUT_403])
    outcome = run_sync("shopify", api_key="k", client=_client(json_body=body))
    assert outcome.ok
    assert outcome.benign_errors == [SHOPIFY_PAYOUT_403]
    assert outcome.fatal_errors == []


def test_benign_error_is_still_surfaced_not_swallowed() -> None:
    """Tolerated must not mean invisible — it's the breadcrumb for removing it."""
    outcome = summarize("shopify", _body(errors=[SHOPIFY_PAYOUT_403]))
    assert outcome.benign_errors  # retained on the outcome, not dropped


def test_benign_and_fatal_together_still_fails() -> None:
    body = _body(errors=[SHOPIFY_PAYOUT_403, "[shopify] connection reset"])
    outcome = run_sync("shopify", api_key="k", client=_client(json_body=body))
    assert not outcome.ok
    assert outcome.fatal_errors == ["[shopify] connection reset"]


def test_unrelated_403_is_not_benign() -> None:
    """The allowlist matches one specific known condition, not all 403s."""
    assert not is_benign("[shopify] 403 Forbidden: read_orders scope not approved")


# ── transport / protocol failures ────────────────────────────────────────────


def test_lock_conflict_raises() -> None:
    with pytest.raises(SyncError, match="already running"):
        run_sync("stripe", api_key="k", client=_client(status_code=409))


def test_http_error_raises() -> None:
    with pytest.raises(SyncError, match="HTTP 500"):
        run_sync("stripe", api_key="k", client=_client(status_code=500, text="boom"))


def test_unauthorized_raises() -> None:
    with pytest.raises(SyncError, match="HTTP 401"):
        run_sync("stripe", api_key="bad", client=_client(status_code=401, text="nope"))


def test_connection_failure_raises() -> None:
    with pytest.raises(SyncError, match="failed"):
        run_sync(
            "stripe",
            api_key="k",
            client=_client(raises=httpx.ConnectError("refused")),
        )


def test_non_json_response_raises() -> None:
    with pytest.raises(SyncError, match="non-JSON"):
        run_sync("stripe", api_key="k", client=_client(text="<html>502</html>"))


# ── CLI contract ─────────────────────────────────────────────────────────────


def test_dry_run_is_the_default_and_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _explode(*_a: object, **_k: object) -> None:
        raise AssertionError("dry-run must not call the endpoint")

    monkeypatch.setattr("scripts.adapter_sync.run_sync", _explode)
    assert main(["--source", "stripe"]) == 0
    assert "DRY-RUN" in capsys.readouterr().out


def test_missing_api_key_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    assert main(["--source", "stripe", "--apply"]) == 2


def test_apply_returns_zero_on_clean_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setattr(
        "scripts.adapter_sync.run_sync",
        lambda *a, **k: summarize("stripe", _body()),
    )
    assert main(["--source", "stripe", "--apply"]) == 0


def test_apply_returns_nonzero_when_records_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "k")
    body = _body(adapter_results=[{"source": "stripe", "records_failed": 1}])
    monkeypatch.setattr(
        "scripts.adapter_sync.run_sync", lambda *a, **k: summarize("stripe", body)
    )
    assert main(["--source", "stripe", "--apply"]) == 1


def test_apply_returns_nonzero_on_sync_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "k")

    def _raise(*_a: object, **_k: object) -> None:
        raise SyncError("boom")

    monkeypatch.setattr("scripts.adapter_sync.run_sync", _raise)
    assert main(["--source", "stripe", "--apply"]) == 1


def test_plaid_is_not_a_supported_source() -> None:
    """Plaid has dedicated sync units; driving it here would double-advance the cursor."""
    assert "plaid" not in SUPPORTED_SOURCES
    with pytest.raises(SystemExit):
        main(["--source", "plaid", "--apply"])


def test_allowlist_is_not_empty_by_accident() -> None:
    """Guards against a future edit emptying the tuple and silently changing policy."""
    assert BENIGN_ERROR_PATTERNS
