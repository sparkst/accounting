"""Tests for src/alerts/retry.py — REQ-FIX-ALR-001.

Fake sleep/rand injected everywhere so no test waits on wall-clock time. Uses
real ``httpx.Response`` objects (constructed with just a status code) rather
than a stand-in class, so the fakes satisfy ``post_with_retry``'s real type
signature exactly.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from src.alerts.retry import post_with_retry


def _fake_sleep() -> tuple[list[float], Callable[[float], None]]:
    delays: list[float] = []

    def sleep(d: float) -> None:
        delays.append(d)

    return delays, sleep


def test_5xx_then_5xx_then_200_succeeds_with_two_sleeps_and_jittered_backoff() -> None:
    responses = iter([httpx.Response(500), httpx.Response(502), httpx.Response(200)])
    calls = {"n": 0}

    def send() -> httpx.Response:
        calls["n"] += 1
        return next(responses)

    delays, sleep = _fake_sleep()
    resp = post_with_retry(send, base_delay=1.0, sleep=sleep, rand=lambda: 0.5)

    assert resp.status_code == 200
    assert calls["n"] == 3
    assert len(delays) == 2
    # attempt 0 -> 1: base_delay * 2**0 + 0.5*base_delay = 1.5
    # attempt 1 -> 2: base_delay * 2**1 + 0.5*base_delay = 2.5
    assert delays[0] == pytest.approx(1.5)
    assert delays[1] == pytest.approx(2.5)


def test_4xx_returns_immediately_no_retry() -> None:
    calls = {"n": 0}

    def send() -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    delays, sleep = _fake_sleep()
    resp = post_with_retry(send, sleep=sleep, rand=lambda: 0.0)

    assert resp.status_code == 404
    assert calls["n"] == 1
    assert delays == []


def test_timeout_exhausts_all_attempts_and_raises() -> None:
    calls = {"n": 0}

    def send() -> httpx.Response:
        calls["n"] += 1
        raise httpx.TimeoutException("boom")

    delays, sleep = _fake_sleep()
    with pytest.raises(httpx.TimeoutException):
        post_with_retry(send, sleep=sleep, rand=lambda: 0.0)

    assert calls["n"] == 3
    assert len(delays) == 2


def test_transport_error_exhausts_all_attempts_and_raises() -> None:
    def send() -> httpx.Response:
        raise httpx.ConnectError("boom")

    delays, sleep = _fake_sleep()
    with pytest.raises(httpx.ConnectError):
        post_with_retry(send, sleep=sleep, rand=lambda: 0.0)


def test_5xx_exhausts_all_attempts_returns_last_response() -> None:
    calls = {"n": 0}

    def send() -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    delays, sleep = _fake_sleep()
    resp = post_with_retry(send, sleep=sleep, rand=lambda: 0.0)

    assert resp.status_code == 503
    assert calls["n"] == 3


def test_success_on_first_attempt_never_sleeps() -> None:
    delays, sleep = _fake_sleep()
    resp = post_with_retry(lambda: httpx.Response(200), sleep=sleep, rand=lambda: 0.0)
    assert resp.status_code == 200
    assert delays == []
