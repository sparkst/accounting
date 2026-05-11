"""Fernet symmetric encryption helpers for Plaid access tokens.

REQ-025: Plaid access_tokens are stored encrypted-at-rest in
``plaid_item.access_token_encrypted``. The encryption key (base64-urlsafe
Fernet key) is held in Doppler and must NEVER be written to disk in plaintext.

The env var is ``PLAID_FERNET_KEY`` (preferred, post-wealth-Cloudflare
migration M0c rename). For backwards compatibility during the migration
window, ``PLAID_TOKEN_ENC_KEY`` is also accepted; if both are set,
``PLAID_FERNET_KEY`` wins. After the wealth migration cutover closes,
``PLAID_TOKEN_ENC_KEY`` in the local accounting/dev Doppler config will be
gone — the local Python only ever talks to Fernet ciphertexts; the AES-GCM
``PLAID_TOKEN_ENC_KEY`` used by the Workers backend lives in Workers Pages
secrets, not the local environment.

Threat model: compromise of the SQLite DB alone does not expose tokens unless
Doppler is also compromised. Compromise of Doppler alone does not expose tokens
unless the DB is also stolen. Both leaking simultaneously is the worst case and
is not separately mitigated (acceptable for single-user personal scope per
spec "Known gaps + accepted risks" #11).

Tokens are decrypted ONLY inside the sync worker context, never logged, and
the plaintext should not be stashed in module-level state.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_PRIMARY_ENV_VAR = "PLAID_FERNET_KEY"
_LEGACY_ENV_VAR = "PLAID_TOKEN_ENC_KEY"


class PlaidCryptoError(RuntimeError):
    """Raised when token encryption/decryption fails for any reason.

    Subclassed so callers can catch this without masking unrelated runtime
    errors (e.g. SQLAlchemy session errors).
    """


class MissingKeyError(PlaidCryptoError):
    """Raised when neither ``PLAID_FERNET_KEY`` nor ``PLAID_TOKEN_ENC_KEY`` is set."""


class InvalidCiphertextError(PlaidCryptoError):
    """Raised when a ciphertext cannot be decrypted with the configured key.

    Most likely cause: the key was rotated and the row was not re-encrypted.
    Run ``scripts/rotate_plaid_key.py`` to re-encrypt rows under the new key.
    """


def _load_keys() -> list[str]:
    """Read keys from env. Comma-separated for MultiFernet rotation windows.

    Looks for ``PLAID_FERNET_KEY`` first (the post-M0c name), then falls back
    to ``PLAID_TOKEN_ENC_KEY`` (the pre-M0c name) so the local Python keeps
    working during the wealth migration's M0c → cutover transition.

    The FIRST key in the list is the active (encryption) key. Subsequent keys
    are decryption-only fallbacks during rotation. Trailing/leading whitespace
    is stripped per-key so accidental spaces in Doppler do not break Fernet.
    """
    raw = os.environ.get(_PRIMARY_ENV_VAR) or os.environ.get(_LEGACY_ENV_VAR)
    if not raw:
        raise MissingKeyError(
            f"Neither {_PRIMARY_ENV_VAR} nor {_LEGACY_ENV_VAR} is set. "
            "Configure one in Doppler (Fernet.generate_key().decode()) before "
            "encrypting Plaid tokens."
        )
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise MissingKeyError(
            f"Plaid Fernet key env var is set but empty after parsing."
        )
    return keys


def _multi_fernet() -> MultiFernet:
    keys = _load_keys()
    try:
        return MultiFernet([Fernet(k.encode("utf-8")) for k in keys])
    except (ValueError, TypeError) as exc:
        raise PlaidCryptoError(
            f"Plaid Fernet key is not valid (expected base64-urlsafe 32 bytes)."
        ) from exc


def encrypt_token(plaintext: str) -> str:
    """Encrypt a Plaid access_token with the active key. Returns ciphertext (str).

    The active key is the FIRST entry in the comma-separated key list.
    """
    if not isinstance(plaintext, str):
        raise PlaidCryptoError("encrypt_token expects a str")
    fern = _multi_fernet()
    return fern.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a Plaid access_token. Raises ``InvalidCiphertextError`` on mismatch.

    Tries each key in the comma-separated list in order; succeeds on the first
    match. This is the standard MultiFernet rotation pattern.
    """
    if not isinstance(ciphertext, str):
        raise PlaidCryptoError("decrypt_token expects a str")
    fern = _multi_fernet()
    try:
        return fern.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise InvalidCiphertextError(
            "Failed to decrypt token with any configured key. Was the key rotated?"
        ) from exc
