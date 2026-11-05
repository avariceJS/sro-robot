"""Bind Tesseract word geometry to parser source spans for raster inputs."""

from __future__ import annotations

import csv
import re
import math
import xml.etree.ElementTree as ET
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


def _unrotate_word(word: SourceWord, clockwise: int) -> SourceWord:
    """Map an OCR box back into the decoded photo's normalized preview frame."""
    x, y, width, height = word.x, word.y, word.width, word.height
    if clockwise == 90:
        return SourceWord(word.start, word.end, word.page,
                          y, 1 - x - width, height, width)
    if clockwise == 180:
        return SourceWord(word.start, word.end, word.page,
                          1 - x - width, 1 - y - height, width, height)
    if clockwise == 270:
        return SourceWord(word.start, word.end, word.page,
                          1 - y - height, x, height, width)
    raise ValueError("Unsupported OCR photo rotation")


class SourceLocationCollector:
    """Per-recognition word boxes, indexed by returned OCR variant."""

    def __init__(self) -> None:
        self._variants: dict[int, list[SourceWord]] = {}
        self._texts: dict[int, str] = {}

    def record_pdf_text(self, index: int, text: str, bbox_xml: str) -> None:
        """Align Poppler words to the unchanged layout text, page by page."""
        normalized = _clean(text)
        pages = normalized.split("\f")
        root = ET.fromstring(bbox_xml)
        words: list[SourceWord] = []
        offset = 0
        for page_number, page in enumerate(root.findall(".//{*}page"), 1):
            if page_number > len(pages):
                break
            page_text = pages[page_number - 1]
            cursor = 0
            pw, ph = float(page.attrib["width"]), float(page.attrib["height"])
            if not (math.isfinite(pw) and math.isfinite(ph) and pw > 0 and ph > 0):
                raise ValueError("Invalid PDF dimensions")
            # bbox-layout groups words by text flow; -layout interleaves those
            # flows by physical line. Restore physical reading order first.
            ordered = sorted(page.findall(".//{*}word"),
                             key=lambda node: (float(node.attrib["yMin"]), float(node.attrib["xMin"])))
            lines: list[list] = []
            for node in ordered:
                top, bottom = float(node.attrib["yMin"]), float(node.attrib["yMax"])
                if lines and abs(top - float(lines[-1][0].attrib["yMin"])) < (bottom - top) * 0.45:
                    lines[-1].append(node)
                else:
                    lines.append([node])
            ordered = [node for line in lines for node in sorted(line, key=lambda node: float(node.attrib["xMin"]))]
            for node in ordered:
                token = _clean("".join(node.itertext())).strip()
                if not token:
                    continue
                start = page_text.find(token, cursor)
                if start < 0:
                    continue
                left, top, right, bottom = (float(node.attrib[key]) for key in ("xMin", "yMin", "xMax", "yMax"))
                if not all(math.isfinite(v) for v in (left, top, right, bottom)):
                    continue
                if not (0 <= left < right <= pw and 0 <= top < bottom <= ph):
                    continue
                words.append(SourceWord(offset + start, offset + start + len(token), page_number,
                                        left / pw, top / ph, (right - left) / pw, (bottom - top) / ph))
                cursor = start + len(token)
            offset += len(page_text) + 1
        self._texts[index] = normalized
        self._variants[index] = words

    def record_variant(
        self, index: int, pages: list[tuple[str, str, tuple[int, int]]],
        *, rotations: list[int] | None = None,
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
            rotation = rotations[page_number - 1] if rotations is not None else 0
            if rotation:
                page_words = [_unrotate_word(word, rotation) for word in page_words]
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
        self._texts[index] = whole

    def boxes_for_span(self, variant: int | None, start: int | None, end: int | None) -> list[dict[str, int | float]]:
        if variant is None or start is None or end is None or end <= start:
            return []
        return [word.as_dict() for word in self._variants.get(variant, ())
                if word.start < end and word.end > start]

    def boxes_for_field(self, attr: str, variant: int | None, start: int | None, end: int | None) -> list[dict[str, int | float]]:
        # Name fields share the parser's full FIO span. Reuse its exact name
        # grammar to locate each component, including a parenthesized old surname.
        component = {"last_name": 1, "first_name": 2, "middle_name": 3}.get(attr)
        if component is not None and variant is not None and start is not None and end is not None:
            from .extract import NAME_TRIPLE
            source = self._texts.get(variant, "")
            match = re.fullmatch(NAME_TRIPLE.pattern, source[start:end], re.IGNORECASE)
            if match is not None:
                return self.boxes_for_span(variant, start + match.start(component), start + match.end(component))
            # A parser may already give a single component's exact span. Keep
            # that location, but never substitute a whole ambiguous name block.
            from .extract import WORD
            if re.fullmatch(WORD, source[start:end], re.IGNORECASE) is None:
                return []
        return self.boxes_for_span(variant, start, end)

    def bind_card(self, card: Any) -> None:
        for attr, value in vars(card).items():
            if hasattr(value, "source_start") and hasattr(value, "source_end"):
                value.source_boxes = self.boxes_for_field(
                    attr, getattr(value, "source_variant", None), value.source_start, value.source_end,
                )
        for suggestion in getattr(card, "review_suggestions", ()):
            for attr, field in suggestion.get("fields", {}).items():
                field["source_boxes"] = self.boxes_for_field(
                    attr, field.get("source_variant"), field.get("source_start"), field.get("source_end"),
                )
