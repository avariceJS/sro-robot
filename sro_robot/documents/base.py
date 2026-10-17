"""Контракт профиля документа."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..core.models import DebtorCard


@dataclass(frozen=True)
class DocumentProfile:
    """Как из файла этого типа получить `DebtorCard`."""

    name: str
    title: str
    parse_file: Callable[..., DebtorCard]

    def parse(self, path, dpi: int = 300, **options) -> DebtorCard:
        return self.parse_file(path, dpi=dpi, **options)
