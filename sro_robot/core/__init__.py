"""Разбор документов: OCR, извлечение полей, морфология, адреса, проверки.

Слой не знает ни о браузере, ни о платформе СРО.
"""

from __future__ import annotations

from .models import AddressKind, DebtorCard, Field

__all__ = ["AddressKind", "DebtorCard", "Field"]
