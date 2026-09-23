"""Определение арбитражного суда о принятии заявления о банкротстве.

Правила извлечения пока живут в `core/extract.py` целиком: это единственный тип
документа, под который они написаны.
"""

from __future__ import annotations

from ..core.extract import parse_file
from .base import DocumentProfile

COURT_RULING = DocumentProfile(
    name="court_ruling",
    title="Определение о принятии заявления",
    parse_file=parse_file,
)
