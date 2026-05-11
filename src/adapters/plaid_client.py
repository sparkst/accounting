"""Plaid client + error classification + retry wrapper.

REQ-026: classify Plaid ``ApiException`` error codes as retryable or terminal
per the table in ``docs/superpowers/specs/2026-05-09-plaid-net-worth-integration.md``.
Retryable failures get exponential backoff (1s/5s/30s, 3 attempts max). Terminal
failures are surfaced immediately so the UI can prompt for re-link.

The Plaid SDK exposes ``plaid.exceptions.ApiException`` with a JSON ``body``
field containing ``error_code``. We don't depend on HTTP status alone — Plaid
returns 400 for both rate-limiting and login-required, and the ``error_code`` is
the only reliable discriminator.

NEVER log the raw ``body`` — it can include institution details. Log ``error_code``
only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable

from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration, Environment
from plaid.exceptions import ApiException

logger = logging.getLogger(__name__)

# Retry tunables. Kept module-level so tests can monkeypatch.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 5.0, 30.0)


# ── Error classification ─────────────────────────────────────────────────────

RETRYABLE_ERROR_CODES = frozenset(
    {
        "RATE_LIMIT_EXCEEDED",
        "INTERNAL_SERVER_ERROR",
        "PLANNED_MAINTENANCE",
        "INSTITUTION_DOWN",
        "INSTITUTION_NOT_RESPONDING",
        "PRODUCT_NOT_READY",
    }
)

# Terminal — surface re-link prompt. Caller must NOT retry.
TERMINAL_ERROR_CODES = frozenset(
    {
        "ITEM_LOGIN_REQUIRED",
        "INVALID_CREDENTIALS",
        "ITEM_LOCKED",
        "INVALID_ACCESS_TOKEN",
        "ACCESS_NOT_GRANTED",
        # NOT in the spec table but is irrecoverable: revoked/removed item.
        "ITEM_NOT_FOUND",
    }
)

# Subset of TERMINAL_ERROR_CODES that surface a user-actionable "re-link"
# prompt. The health-dashboard and weekly-P&L alert paths import this so all
# three surfaces stay consistent — adding a new terminal code in ONE place
# propagates to dashboard + email automatically.
RELINK_REQUIRED_CODES = frozenset(
    {"ITEM_LOGIN_REQUIRED", "INVALID_CREDENTIALS", "ITEM_LOCKED", "INVALID_ACCESS_TOKEN"}
)


class PlaidErrorBase(Exception):
    """Base for retryable / terminal Plaid errors with a normalized ``error_code``.

    Wraps the SDK's ``ApiException`` so the rest of the code never has to read
    ``ApiException.body`` directly (which leaks institution info if logged).
    """

    def __init__(self, error_code: str, message: str | None = None) -> None:
        self.error_code = error_code
        self.message = message or error_code
        super().__init__(self.message)


class RetryablePlaidError(PlaidErrorBase):
    """Plaid said try again later. The retry helper will retry; if exhausted,
    the caller marks the Item ``last_sync_status='error'`` with retryable=True.
    """


class TerminalPlaidError(PlaidErrorBase):
    """Plaid said you'll never succeed without user intervention. Don't retry.
    Caller surfaces the re-link prompt in the UI.
    """


class UnknownPlaidError(TerminalPlaidError):
    """A Plaid error code we don't recognize. Treated as terminal (fail-safe):
    we'd rather a stranger error stop the sync loudly than be silently retried
    forever or silently swallowed.
    """


def _extract_error_code(exc: ApiException) -> str:
    """Best-effort error_code parse from ApiException.body. Returns 'UNKNOWN' if not parseable."""
    body = getattr(exc, "body", None)
    if not body:
        return "UNKNOWN"
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        return str(json.loads(body).get("error_code", "UNKNOWN"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return "UNKNOWN"


def classify_plaid_error(exc: ApiException) -> PlaidErrorBase:
    """Normalize an SDK ApiException into the project's typed Plaid error.

    Returns the typed error; the caller decides whether to raise or retry.
    """
    code = _extract_error_code(exc)
    if code in RETRYABLE_ERROR_CODES:
        return RetryablePlaidError(code, message=f"Plaid retryable: {code}")
    if code in TERMINAL_ERROR_CODES:
        return TerminalPlaidError(code, message=f"Plaid terminal: {code}")
    return UnknownPlaidError(code, message=f"Plaid unknown error_code: {code}")


# ── Retry wrapper ────────────────────────────────────────────────────────────


def call_with_retry[T](  # noqa: UP047 — PEP 695 syntax already used
    fn: Callable[[], T],
    *,
    backoff: tuple[float, ...] = RETRY_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn``. On RetryablePlaidError, sleep ``backoff[attempt]`` and try again.

    On TerminalPlaidError, raise immediately. On UnknownPlaidError, raise (no retry).
    Other exceptions propagate unchanged.

    ``backoff`` is the *gap before each attempt after the first*: a tuple of 3
    floats means up to 4 total attempts (1 initial + 3 retries). Default is
    spec's 1s/5s/30s (3 retries = 4 attempts total).

    ``sleep`` is overridable so tests don't actually wait.
    """
    max_retries = len(backoff)
    attempt = 0
    while True:
        try:
            return fn()
        except ApiException as raw:
            typed = classify_plaid_error(raw)
            if isinstance(typed, RetryablePlaidError):
                if attempt >= max_retries:
                    logger.warning(
                        "plaid retryable exhausted",
                        extra={"error_code": typed.error_code, "attempts": attempt + 1},
                    )
                    raise typed from raw
                wait = backoff[attempt]
                logger.info(
                    "plaid retryable, sleeping",
                    extra={
                        "error_code": typed.error_code,
                        "attempt": attempt + 1,
                        "wait_s": wait,
                    },
                )
                sleep(wait)
                attempt += 1
                continue
            # Terminal or Unknown: raise immediately.
            raise typed from raw


# ── Client factory ────────────────────────────────────────────────────────────


def _resolve_environment(env_name: str) -> str:
    """Map a string env name to a Plaid SDK Environment host URL.

    Plaid removed the "development" environment in 2024; we accept the legacy
    spelling for backward-compat but route it to Sandbox with a warning.

    Returns the host URL string (e.g. "https://production.plaid.com"). The
    Plaid SDK's ``Environment.Production`` / ``.Sandbox`` are str constants;
    we stringify them explicitly so mypy sees a concrete ``str`` type.
    """
    env_lower = env_name.strip().lower()
    if env_lower in ("production", "prod"):
        return str(Environment.Production)
    if env_lower in ("sandbox", "dev", "development"):
        if env_lower in ("dev", "development"):
            logger.warning(
                "PLAID_ENV=%r mapped to Sandbox (Plaid retired 'development' env)",
                env_name,
            )
        return str(Environment.Sandbox)
    raise ValueError(
        f"PLAID_ENV={env_name!r} not recognized. Use 'sandbox' or 'production'."
    )


def make_plaid_client() -> plaid_api.PlaidApi:
    """Build a configured Plaid SDK client from Doppler-managed env.

    Required env:
    - ``PLAID_CLIENT_ID``
    - ``PLAID_ENV`` (sandbox|production)
    - ``PLAID_SANDBOX_SECRET`` OR ``PLAID_PRODUCTION_SECRET`` (whichever matches PLAID_ENV)
    """
    client_id = os.environ.get("PLAID_CLIENT_ID")
    env_name = os.environ.get("PLAID_ENV", "sandbox")
    env = _resolve_environment(env_name)

    if not client_id:
        raise RuntimeError("PLAID_CLIENT_ID is required (set via Doppler)")

    secret_var = (
        "PLAID_PRODUCTION_SECRET"
        if env == str(Environment.Production)
        else "PLAID_SANDBOX_SECRET"
    )
    secret = os.environ.get(secret_var)
    if not secret:
        raise RuntimeError(
            f"{secret_var} is required for PLAID_ENV={env_name} (set via Doppler)"
        )

    config = Configuration(
        host=env,
        api_key={"clientId": client_id, "secret": secret},
    )
    return plaid_api.PlaidApi(ApiClient(config))
