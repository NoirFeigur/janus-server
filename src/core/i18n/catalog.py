"""i18n 词条目录:启动时加载 ``src/locales/{locale}/*.json``,运行时按 key 翻译。

架构决策 6.12.4 的后端 ``I18n`` 类落地。所有 ``locales/{locale}/`` 下的 JSON
文件在进程启动时被读入并**扁平合并**为单个 ``locale → {key: template}`` 字典
(各 JSON 已是 flat key,如 ``enum.userStatus.active`` / ``validation.missing``,
天然不冲突)。

翻译走三级回退(6.12.4):
1. 目标 locale 命中;
2. 否则 ``default_locale`` 命中;
3. 仍未命中 → 原样返回 key(便于前端/日志定位缺失词条)。

命中模板后用 ``str.format(**params)`` 插值;缺占位符或多余参数都不抛(用
:class:`_SafeDict` 把未提供的占位符原样保留),避免一条缺参的词条让整个
错误响应 500。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from src.config import get_settings

# src/core/i18n/catalog.py → parents[2] == src/ ;词条根目录 src/locales/。
_LOCALES_DIR = Path(__file__).resolve().parents[2] / "locales"


class _SafeDict(dict[str, object]):
    """``str.format_map`` 的缺键安全字典:未提供的占位符原样保留为 ``{key}``。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class I18n:
    """加载并持有全部 locale 词条,提供按 key 翻译与按前缀批量取词。"""

    def __init__(self, catalog: dict[str, dict[str, str]], default_locale: str) -> None:
        self._catalog = catalog
        self._default_locale = default_locale

    @classmethod
    def load(cls, locales_dir: Path | None = None) -> I18n:
        """从磁盘加载词条。``locales_dir`` 仅测试注入用,生产用默认根目录。"""
        settings = get_settings()
        root = locales_dir or _LOCALES_DIR
        catalog: dict[str, dict[str, str]] = {}
        for locale in settings.supported_locales:
            merged: dict[str, str] = {}
            locale_dir = root / locale
            if locale_dir.is_dir():
                for json_file in sorted(locale_dir.glob("*.json")):
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError(f"i18n catalog {json_file} must be a flat JSON object")
                    merged.update({str(k): str(v) for k, v in data.items()})
            catalog[locale] = merged
        return cls(catalog, settings.default_locale)

    def t(self, key: str, locale: str | None = None, /, **params: object) -> str:
        """翻译 ``key``:三级回退(locale → default → key),命中后插值。"""
        target = locale or self._default_locale
        template = (
            self._catalog.get(target, {}).get(key)
            or self._catalog.get(self._default_locale, {}).get(key)
            or key
        )
        if not params:
            return template
        return template.format_map(_SafeDict(params))

    def prefixed(self, prefix: str, locale: str | None = None) -> dict[str, str]:
        """取某前缀(如 ``enum.``)下该 locale 的全部词条,缺失回退 default。"""
        target = locale or self._default_locale
        base = self._catalog.get(self._default_locale, {})
        override = self._catalog.get(target, {})
        out: dict[str, str] = {k: v for k, v in base.items() if k.startswith(prefix)}
        out.update({k: v for k, v in override.items() if k.startswith(prefix)})
        return out


@lru_cache(maxsize=1)
def get_i18n() -> I18n:
    """进程级单例:首次调用时加载磁盘词条,之后复用。"""
    return I18n.load()
