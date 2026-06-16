from contextvars import ContextVar

from src.config import get_settings

_current_locale: ContextVar[str] = ContextVar("locale", default="zh-CN")


def normalize_locale(locale: str | None) -> str:
    settings = get_settings()
    if not locale:
        return settings.default_locale
    normalized = locale.strip()
    if normalized in settings.supported_locales:
        return normalized
    language = normalized.split(",", maxsplit=1)[0].split("-", maxsplit=1)[0].lower()
    for candidate in settings.supported_locales:
        if candidate.lower().startswith(language):
            return candidate
    return settings.default_locale


def get_locale() -> str:
    return _current_locale.get()


def set_locale(locale: str | None) -> None:
    _current_locale.set(normalize_locale(locale))
