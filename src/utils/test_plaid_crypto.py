"""Tests for src/utils/plaid_crypto.py — REQ-025 (encrypted access_token at rest)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from src.utils.plaid_crypto import (
    InvalidCiphertextError,
    MissingKeyError,
    PlaidCryptoError,
    decrypt_token,
    encrypt_token,
)


def _set_key(monkeypatch: pytest.MonkeyPatch, key: str | None) -> None:
    if key is None:
        monkeypatch.delenv("PLAID_TOKEN_ENC_KEY", raising=False)
    else:
        monkeypatch.setenv("PLAID_TOKEN_ENC_KEY", key)


def test_round_trip_under_active_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """encrypt_token() output decrypts back to the original plaintext."""
    key = Fernet.generate_key().decode()
    _set_key(monkeypatch, key)

    token = "access-sandbox-deadbeef-1234"
    ct = encrypt_token(token)
    assert ct != token  # ciphertext is not the plaintext
    assert decrypt_token(ct) == token


def test_round_trip_is_not_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fernet includes a random IV so two ciphertexts of the same input differ."""
    key = Fernet.generate_key().decode()
    _set_key(monkeypatch, key)

    a = encrypt_token("same-token")
    b = encrypt_token("same-token")
    assert a != b
    assert decrypt_token(a) == "same-token"
    assert decrypt_token(b) == "same-token"


def test_missing_key_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env raises MissingKeyError (not KeyError)."""
    _set_key(monkeypatch, None)
    with pytest.raises(MissingKeyError):
        encrypt_token("foo")
    with pytest.raises(MissingKeyError):
        decrypt_token("foo")


def test_empty_key_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only env raises MissingKeyError, not a generic failure."""
    _set_key(monkeypatch, "   ,  ,   ")
    with pytest.raises(MissingKeyError):
        encrypt_token("foo")


def test_invalid_key_format_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-Fernet key raises PlaidCryptoError (not a bare ValueError leak)."""
    _set_key(monkeypatch, "not-a-real-fernet-key")
    with pytest.raises(PlaidCryptoError):
        encrypt_token("foo")


def test_wrong_key_decrypt_raises_invalid_ciphertext(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decrypting under a different key than was used to encrypt raises typed error."""
    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()

    _set_key(monkeypatch, key_a)
    ct = encrypt_token("secret")

    _set_key(monkeypatch, key_b)
    with pytest.raises(InvalidCiphertextError):
        decrypt_token(ct)


def test_multi_fernet_rotation_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """During rotation, both old and new key are listed; decrypt tries both."""
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()

    # Encrypt under the old key alone.
    _set_key(monkeypatch, old)
    ct_old = encrypt_token("old-token")

    # Switch to "new is active, old is fallback" — this is the rotation window.
    _set_key(monkeypatch, f"{new},{old}")
    assert decrypt_token(ct_old) == "old-token"  # old key still works
    ct_new = encrypt_token("new-token")  # new writes use the new key
    assert decrypt_token(ct_new) == "new-token"

    # Drop the old key. The old ciphertext should now fail.
    _set_key(monkeypatch, new)
    with pytest.raises(InvalidCiphertextError):
        decrypt_token(ct_old)
    assert decrypt_token(ct_new) == "new-token"


def test_non_str_input_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: non-str inputs are rejected rather than coerced."""
    key = Fernet.generate_key().decode()
    _set_key(monkeypatch, key)

    with pytest.raises(PlaidCryptoError):
        encrypt_token(b"bytes-not-str")  # type: ignore[arg-type]
    with pytest.raises(PlaidCryptoError):
        decrypt_token(12345)  # type: ignore[arg-type]


def test_writes_during_rotation_use_active_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encrypt during rotation must use the FIRST (active) key, not a fallback.

    Regression guard: if key order ever flipped in MultiFernet, new writes would
    silently encrypt under the OLD key and stop decrypting the moment the old
    key is dropped from rotation.
    """
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()

    # In the rotation window, `new` is active (first), `old` is fallback (second).
    _set_key(monkeypatch, f"{new},{old}")
    ct = encrypt_token("written-during-rotation")

    # Drop the OLD key. If `encrypt_token` had used `old`, this would fail.
    _set_key(monkeypatch, new)
    assert decrypt_token(ct) == "written-during-rotation"
