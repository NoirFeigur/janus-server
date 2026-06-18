"""Tests for channel-key reversible encryption (src/core/channel_crypto.py)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from src.config import get_settings
from src.core import channel_crypto


@pytest.fixture(autouse=True)
def _reset_cipher_cache() -> Iterator[None]:
    """The MultiFernet is lru_cached on settings; clear it around each test."""
    channel_crypto._cipher.cache_clear()
    yield
    channel_crypto._cipher.cache_clear()


@pytest.fixture
def _configure_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure a single valid Fernet key on the settings singleton."""
    key = Fernet.generate_key().decode("ascii")
    settings = get_settings()
    monkeypatch.setattr(settings, "channel_encryption_keys", SecretStr(key))
    channel_crypto._cipher.cache_clear()
    return key


def test_roundtrip_recovers_plaintext(_configure_key: str) -> None:
    secret = "sk-upstream-vendor-key-abc123"
    ciphertext = channel_crypto.encrypt_channel_key(secret)
    assert ciphertext != secret  # actually encrypted, not stored plain
    assert channel_crypto.decrypt_channel_key(ciphertext) == secret


def test_unconfigured_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "channel_encryption_keys", None)
    channel_crypto._cipher.cache_clear()
    with pytest.raises(channel_crypto.ChannelEncryptionError):
        channel_crypto.encrypt_channel_key("anything")


def test_tampered_ciphertext_raises(_configure_key: str) -> None:
    ciphertext = channel_crypto.encrypt_channel_key("secret")
    tampered = ciphertext[:-4] + ("AAAA" if ciphertext[-4:] != "AAAA" else "BBBB")
    with pytest.raises(channel_crypto.ChannelEncryptionError):
        channel_crypto.decrypt_channel_key(tampered)


def test_rotation_decrypts_old_ciphertext(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ciphertext under the old key still decrypts after prepending a new key."""
    old_key = Fernet.generate_key().decode("ascii")
    settings = get_settings()
    monkeypatch.setattr(settings, "channel_encryption_keys", SecretStr(old_key))
    channel_crypto._cipher.cache_clear()
    ciphertext = channel_crypto.encrypt_channel_key("rotated-secret")

    # Rotate: new key first (encrypts), old key retained (decrypts).
    new_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(
        settings, "channel_encryption_keys", SecretStr(f"{new_key},{old_key}")
    )
    channel_crypto._cipher.cache_clear()
    assert channel_crypto.decrypt_channel_key(ciphertext) == "rotated-secret"


def test_malformed_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "channel_encryption_keys", SecretStr("not-a-key"))
    channel_crypto._cipher.cache_clear()
    with pytest.raises(channel_crypto.ChannelEncryptionError):
        channel_crypto.encrypt_channel_key("x")


def test_key_hint_exposes_only_tail() -> None:
    assert channel_crypto.key_hint("sk-secret-a1b2") == "...a1b2"
    assert channel_crypto.key_hint("ab") == "...ab"
