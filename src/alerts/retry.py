"""Shared webhook POST retry with exponential backoff + full jitter.

REQ-FIX-ALR-001: a transient n8n blip (connection error, timeout, 5xx) must
not permanently lose an alert — both n8n webhook clients
(``src/alerts/webhook.py::post_alert`` / ``post_raw_payload`` and
``src/balance_alerts/webhook.py::post_payload``) route their single
``httpx.post`` call through ``post_with_retry`` so the retry policy lives in
exactly one place.

Semantics: attempt -> on ``httpx.TransportError`` (covers
``httpx.TimeoutException``, connection errors, etc.) or a 5xx response, sleep
``base_delay * 2**n + rand() * base_delay`` (full jitter on top of exponential
backoff) and retry, up to ``attempts`` total tries. A 4xx response returns
immediately — it's a caller-side bug, and retrying just spams n8n. The final
failure propagates: the exception re-raises, or the last 5xx response is
returned as-is. Tests inject fake ``sleep``/``rand`` callables so there is
never a real wall-clock wait.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import httpx


def post_with_retry(
    send: Callable[[], httpx.Response],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> httpx.Response:
    """Call ``send()`` up to ``attempts`` times with backoff on transient failures."""
    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            response = send()
        except httpx.TransportError:
            if is_last:
                raise
            sleep(base_delay * (2**attempt) + rand() * base_delay)
            continue

        if response.status_code // 100 == 5:
            if is_last:
                return response
            sleep(base_delay * (2**attempt) + rand() * base_delay)
            continue

        # 2xx/3xx/4xx: return immediately, no retry.
        return response

    # Unreachable — the loop above always returns or raises on the final
    # attempt — but keeps mypy/type-checkers satisfied that every path returns.
    raise AssertionError("post_with_retry: unreachable")
