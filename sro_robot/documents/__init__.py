"""Профили извлечения по типу документа.

Пока профиль один: правила в `core/extract.py` написаны под определение о
принятии заявления. Заявления, решения и документы по назначениям добавляются
сюда отдельными профилями со своими правилами.
"""

from __future__ import annotations

from .base import DocumentProfile
from .court_ruling import COURT_RULING

PROFILES: dict[str, DocumentProfile] = {COURT_RULING.name: COURT_RULING}

DEFAULT_PROFILE = COURT_RULING.name


def get_profile(name: str = DEFAULT_PROFILE) -> DocumentProfile:
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise KeyError(f"Неизвестный профиль «{name}». Известные: {known}") from None


__all__ = ["COURT_RULING", "DEFAULT_PROFILE", "PROFILES", "DocumentProfile", "get_profile"]
