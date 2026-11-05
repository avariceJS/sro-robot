"""Ошибки платформы."""

from __future__ import annotations


class SroError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code
