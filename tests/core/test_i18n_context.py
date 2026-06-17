"""Tests for locale normalization + context (src/core/i18n/context.py).

Covers every ``normalize_locale`` branch (exact match, language-prefix match,
fallback to default, empty input) plus the ``ContextVar`` get/set round-trip.
``get_settings`` is ``lru_cache``d with the real defaults (default ``zh-CN``,
supported ``zh-CN`` / ``en-US``), which is exactly what we assert against.
"""

from __future__ import annotations

from src.config import get_settings
from src.core.i18n.context import get_locale, normalize_locale, set_locale


def test_normalize_empty_returns_default() -> None:
    assert normalize_locale(None) == get_settings().default_locale
    assert normalize_locale("") == get_settings().default_locale


def test_normalize_exact_supported_match() -> None:
    assert normalize_locale("en-US") == "en-US"
    assert normalize_locale("zh-CN") == "zh-CN"


def test_normalize_strips_whitespace() -> None:
    assert normalize_locale("  en-US  ") == "en-US"


def test_normalize_language_prefix_match() -> None:
    """An Accept-Language style value resolves by language prefix."""
    # "en-GB,en;q=0.9" → language "en" → first supported starting with "en".
    assert normalize_locale("en-GB,en;q=0.9") == "en-US"
    # bare "zh" → matches "zh-CN".
    assert normalize_locale("zh") == "zh-CN"


def test_normalize_unknown_language_falls_back_to_default() -> None:
    assert normalize_locale("fr-FR") == get_settings().default_locale
    assert normalize_locale("ja") == get_settings().default_locale


def test_get_locale_default_without_set() -> None:
    # ContextVar default is "zh-CN" (module-level default).
    assert get_locale() == "zh-CN"


def test_set_then_get_locale_round_trip() -> None:
    set_locale("en-US")
    assert get_locale() == "en-US"


def test_set_locale_normalizes_before_storing() -> None:
    set_locale("en-GB,en;q=0.8")
    assert get_locale() == "en-US"
    set_locale("unknown-XX")
    assert get_locale() == get_settings().default_locale
