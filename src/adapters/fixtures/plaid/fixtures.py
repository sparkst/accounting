"""Plaid fixture loader — turns JSON files into duck-typed response objects.

The sync code accesses Plaid responses via attribute (``resp.accounts``,
``account.balances.current``, ``account.to_dict()``) because that's how the
Plaid SDK exposes them. For tests, we don't want to construct full openapi
model instances — we build a ``SimpleNamespace`` tree with the same access
pattern, plus a ``to_dict()`` method that round-trips back to the original
dict (so adapter code that stores ``raw_data`` keeps working).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

FIXTURE_DIR = Path(__file__).parent


def load_fixture_dict(name: str) -> dict[str, Any]:
    """Load a JSON fixture file from the fixtures directory.

    ``name`` may include or omit ``.json``. Raises FileNotFoundError if missing.
    """
    if not name.endswith(".json"):
        name = f"{name}.json"
    path = FIXTURE_DIR / name
    with path.open() as f:
        loaded = json.load(f)
    return cast(dict[str, Any], loaded)


def _wrap(value: Any) -> Any:
    """Recursively wrap dict→SimpleNamespace, list→list-of-wrapped, leaf unchanged.

    Adds a ``.to_dict()`` to every namespace so adapter code that calls
    ``account.to_dict()`` round-trips back to the original mapping.
    """
    if isinstance(value, dict):
        wrapped = {k: _wrap(v) for k, v in value.items()}
        ns = SimpleNamespace(**wrapped)
        # Capture the *unwrapped* original for to_dict so callers see plain
        # JSON-serializable values, not SimpleNamespace.
        original = value
        ns.to_dict = lambda _orig=original: _orig
        return ns
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value


def make_response_from_fixture(name: str) -> SimpleNamespace:
    """Return a duck-typed response object matching the JSON fixture's shape.

    Example:
        resp = make_response_from_fixture("accounts_balance_get_mixed")
        assert resp.accounts[0].account_id == "plaid_acct_chase_checking_0001"
        assert resp.accounts[0].balances.current == 4523.18
        assert resp.accounts[0].to_dict()["name"] == "Chase Total Checking"
    """
    data = load_fixture_dict(name)
    wrapped = _wrap(data)
    assert isinstance(wrapped, SimpleNamespace)  # noqa: S101 — module is test-only
    return wrapped
