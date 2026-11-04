"""Bind Tesseract word geometry to parser source spans for raster inputs."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from io import StringIO
from typing import Any


@dataclass(frozen=True)
class SourceWord:
    start: int
    end: int
    page: int
    x: float
    y: float
    width: float
    height: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "page": self.page, "x": self.x, "y": self.y,
            "width": self.width, "height": self.height,
        }


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\u00a0", " "))


def words_from_tsv(
    tsv: str, recognized_text: str, *, page: int = 1,
    page_size: tuple[int, int] | None = None,
) -> list[SourceWord]:
    """Align valid TSV words to the exact parser-normalized OCR text.

    A word is omitted when its spelling cannot be found in order or its box is
    invalid. This deliberately prefers missing evidence over a guessed box.
    Coordinates are fractions of page dimensions in the 0..1 range.
    """
    normalized = _clean(recognized_text)
    cursor = 0
    result: list[SourceWord] = []
    try:
        rows = csv.DictReader(StringIO(tsv), delimiter="\t")
        for row in rows:
            value = (row.get("text") or "").strip()
            if not value:
                continue
            try:
                left, top = int(row["left"]), int(row["top"])
                width, height = int(row["width"]), int(row["height"])
                page_width = int(row.get("page_width") or (page_size or (0, 0))[0])
                page_height = int(row.get("page_height") or (page_size or (0, 0))[1])
                confidence = float(row.get("conf", "-1"))
            except (KeyError, TypeError, ValueError):
                continue
            if (confidence < 0 or page_width <= 0 or page_height <= 0 or
                    left < 0 or top < 0 or width <= 0 or height <= 0 or
                    left + width > page_width or top + height > page_height):
                continue
            start = normalized.find(value, cursor)
            if start < 0:
                # Tesseract's text output may differ in spacing, but not in
                # token order. Case folding is safe for locating a token.
                match = re.search(re.escape(value), normalized[cursor:], re.IGNORECASE)
                if match is None:
                    continue
                start = cursor + match.start()
                end = cursor + match.end()
            else:
                end = start + len(value)
            if start < cursor or end <= start:
                continue
            result.append(SourceWord(
                start, end, page, left / page_width, top / page_height,
                width / page_width, height / page_height,
            ))
            cursor = end
    except (csv.Error, TypeError):
        return []
    return result


class SourceLocationCollector:
    """Per-recognition word boxes, indexed by returned OCR variant."""

    def __init__(self) -> None:
        self._variants: dict[int, list[SourceWord]] = {}

    def record_variant(
        self, index: int, pages: list[tuple[str, str, tuple[int, int]]],
    ) -> None:
        """Record (recognized page text, TSV) pairs for one output variant."""
        if index < 0:
            return
        chunks: list[str] = []
        words: list[SourceWord] = []
        offset = 0
        for page_number, (text, tsv, page_size) in enumerate(pages, start=1):
            if page_number > 1:
                chunks.append("\n")
                offset += 1
            normalized_page = _clean(text)
            chunks.append(normalized_page)
            page_words = words_from_tsv(tsv, text, page=page_number, page_size=page_size)
            words.extend(SourceWord(
                word.start + offset, word.end + offset, word.page,
                word.x, word.y, word.width, word.height,
            ) for word in page_words)
            offset += len(normalized_page)
        raw = "".join(chunks)
        whole = raw.strip()
        # OCR variants are stripped before parse(); compensate for that leading
        # whitespace removal when mapping word offsets into parser spans.
        leading = len(raw) - len(raw.lstrip())
        words = [SourceWord(w.start - leading, w.end - leading, w.page,
                            w.x, w.y, w.width, w.height)
                 for w in words if w.start >= leading]
        self._variants[index] = words if whole else []

    def boxes_for_span(self, variant: int | None, start: int | None, end: int | None) -> list[dict[str, int | float]]:
        if variant is None or start is None or end is None or end <= start:
            return []
        return [word.as_dict() for word in self._variants.get(variant, ())
                if word.start < end and word.end > start]

    def bind_card(self, card: Any) -> None:
        for value in vars(card).values():
            if hasattr(value, "source_start") and hasattr(value, "source_end"):
                value.source_boxes = self.boxes_for_span(
                    getattr(value, "source_variant", None), value.source_start, value.source_end,
                )
        for suggestion in getattr(card, "review_suggestions", ()):
            for field in suggestion.get("fields", {}).values():
                field["source_boxes"] = self.boxes_for_span(
                    field.get("source_variant"), field.get("source_start"), field.get("source_end"),
                )
