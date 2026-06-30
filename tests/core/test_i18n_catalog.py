"""Tests for the i18n catalog/loader (src/core/i18n/catalog.py).

Covers the ``I18n`` class behaviour against an isolated temp ``locales`` tree
(so the test is independent of the production词条 content): JSON loading + flat
merge across multiple files, the 3-level ``t()`` fallback (locale → default →
key), ``str.format`` interpolation with safe missing-key handling, ``prefixed``
filtering with locale override on top of the default base, and the
non-dict-JSON guard. The ``get_i18n`` singleton is exercised against the real
production词条 to prove the on-disk catalog actually loads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.i18n import get_i18n
from src.core.i18n.catalog import I18n


def _write_catalog(root: Path) -> None:
    zh = root / "zh-CN"
    en = root / "en-US"
    zh.mkdir(parents=True)
    en.mkdir(parents=True)
    (zh / "enums.json").write_text(
        json.dumps({"enum.userStatus.active": "启用"}, ensure_ascii=False),
        encoding="utf-8",
    )
    # A second file proves multiple JSONs flat-merge into one locale dict.
    (zh / "validation.json").write_text(
        json.dumps({"validation.string_too_short": "至少 {min_length} 个字符"}),
        encoding="utf-8",
    )
    (en / "enums.json").write_text(
        json.dumps({"enum.userStatus.active": "Active"}),
        encoding="utf-8",
    )
    # en deliberately omits the validation key → exercises default fallback.


@pytest.fixture
def i18n(tmp_path: Path) -> I18n:
    _write_catalog(tmp_path)
    return I18n.load(locales_dir=tmp_path)


def test_load_merges_multiple_files_per_locale(i18n: I18n) -> None:
    assert i18n.t("enum.userStatus.active", "zh-CN") == "启用"
    assert i18n.t("validation.string_too_short", "zh-CN", min_length=8) == "至少 8 个字符"


def test_translate_target_locale_hit(i18n: I18n) -> None:
    assert i18n.t("enum.userStatus.active", "en-US") == "Active"


def test_translate_falls_back_to_default_locale(i18n: I18n) -> None:
    # en lacks the validation key → falls back to zh-CN (default) template.
    assert i18n.t("validation.string_too_short", "en-US", min_length=3) == "至少 3 个字符"


def test_translate_unknown_key_returns_key(i18n: I18n) -> None:
    assert i18n.t("enum.nope.missing", "en-US") == "enum.nope.missing"


def test_translate_default_locale_when_none(i18n: I18n) -> None:
    # No locale arg → default_locale (zh-CN).
    assert i18n.t("enum.userStatus.active") == "启用"


def test_format_missing_param_left_intact(i18n: I18n) -> None:
    # Template wants {min_length} but none provided → placeholder kept, no raise.
    assert i18n.t("validation.string_too_short", "zh-CN") == "至少 {min_length} 个字符"


def test_format_extra_param_ignored(i18n: I18n) -> None:
    out = i18n.t("enum.userStatus.active", "zh-CN", unused="x")
    assert out == "启用"


def test_prefixed_filters_and_overrides(i18n: I18n) -> None:
    # zh has the enum key; en overrides it. Asking en → English value.
    labels = i18n.prefixed("enum.", "en-US")
    assert labels == {"enum.userStatus.active": "Active"}
    # Validation keys excluded by the prefix filter.
    assert all(k.startswith("enum.") for k in labels)


def test_prefixed_default_base_fills_missing_locale_keys(i18n: I18n) -> None:
    # zh-only key under a prefix should surface even when querying en, via base.
    labels = i18n.prefixed("validation.", "en-US")
    assert labels == {"validation.string_too_short": "至少 {min_length} 个字符"}


def test_load_rejects_non_dict_json(tmp_path: Path) -> None:
    (tmp_path / "zh-CN").mkdir(parents=True)
    (tmp_path / "zh-CN" / "bad.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="flat JSON object"):
        I18n.load(locales_dir=tmp_path)


def test_get_i18n_loads_real_catalog() -> None:
    # The production on-disk词条 must actually load and resolve a known key.
    i18n = get_i18n()
    assert i18n.t("enum.userStatus.active", "zh-CN") == "启用"
    assert i18n.t("enum.userStatus.active", "en-US") == "Active"
    assert i18n.t("validation.missing", "en-US") == "This field is required"
