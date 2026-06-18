"""Reversible encryption for upstream vendor keys (data-model §2.2).

``channel_key.api_key_encrypted`` stores upstream LLM provider keys that the
gateway must decrypt at ``litellm.Router`` build time — the opposite direction
from our own ``api_key`` (those are one-way hashed, never recoverable). Fernet
(AES-128-CBC + HMAC, url-safe base64) gives authenticated symmetric encryption.

Key material lives in ``settings.channel_encryption_keys`` (a ``.env`` secret),
comma-separated for rotation: the **first** key encrypts, **all** keys decrypt
(``MultiFernet``). Rolling rotation: prepend a new key and re-encrypt, then drop
the old one once no ciphertext references it.

Fail-fast: the accessor is built lazily and raises if no key is configured. Only
the gateway touches it at Router-build time; auth/admin management never does, so
a platform running without the gateway needs no channel key.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from src.config import get_settings


class ChannelEncryptionError(RuntimeError):
    """Raised when channel encryption is unconfigured or a ciphertext is invalid."""


@lru_cache
def _cipher() -> MultiFernet:
    """Build the process-wide MultiFernet from configured keys (fail-fast).

    First key encrypts; all keys decrypt (rotation). Raises
    :class:`ChannelEncryptionError` when no key is configured.
    """
    raw = get_settings().channel_encryption_keys
    if raw is None:
        raise ChannelEncryptionError(
            "channel encryption key is not configured "
            "(set JANUS_CHANNEL_ENCRYPTION_KEYS)"
        )
    keys = [k.strip() for k in raw.get_secret_value().split(",") if k.strip()]
    if not keys:
        raise ChannelEncryptionError(
            "channel encryption key is empty "
            "(set JANUS_CHANNEL_ENCRYPTION_KEYS)"
        )
    try:
        fernets = [Fernet(k.encode("ascii")) for k in keys]
    except (ValueError, TypeError) as exc:
        raise ChannelEncryptionError(
            "channel encryption key is malformed "
            "(must be 32-byte url-safe base64 from Fernet.generate_key())"
        ) from exc
    return MultiFernet(fernets)


def encrypt_channel_key(plaintext: str) -> str:
    """Encrypt an upstream vendor key for storage. Returns url-safe ciphertext."""
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_channel_key(ciphertext: str) -> str:
    """Decrypt a stored upstream vendor key. Raises on tamper/unknown key."""
    try:
        return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ChannelEncryptionError(
            "channel key ciphertext is invalid "
            "(tampered, or encrypted under a key no longer configured)"
        ) from exc


def key_hint(plaintext: str) -> str:
    """Last 4 chars of the plaintext key, for ops identification (never the key).

    Mirrors ``channel_key.key_hint`` — a non-sensitive tail so operators can tell
    keys apart in the UI without ever exposing the secret.
    """
    tail = plaintext[-4:] if len(plaintext) >= 4 else plaintext
    return f"...{tail}"
