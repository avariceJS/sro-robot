"""Умение работать с платформой СРО: вход, навигация, элементы, реестры.

Слой не знает, какой документ обрабатывается и какая карточка заполняется —
это дело сценариев.
"""

from __future__ import annotations

from .client import SroClient
from .errors import SroError
from .registry import Registry, SearchHit

__all__ = ["Registry", "SearchHit", "SroClient", "SroError"]
