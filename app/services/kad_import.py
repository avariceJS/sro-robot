"""Validate browser-acquired sources and bind them to one local task occurrence."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from sro_robot.core.validators import normalize_case_number

SOURCE_KEY = "_kad_source_v1"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class KadDocumentMismatch(ValueError):
    """The local parser did not confirm the requested case."""


def validate_source(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("invalid_source")
    number = value.get("caseNumber")
    if not isinstance(number, str) or not re.fullmatch(r"А\d{1,3}-\d+/\d{4}", number) or len(number) > 64:
        raise ValueError("invalid_case")
    run = value.get("requestId")
    if not isinstance(run, str) or not re.fullmatch(UUID, run, re.I):
        raise ValueError("invalid_run")

    def url(raw: str, pattern: str) -> str:
        if not isinstance(raw, str) or len(raw) > 2048 or re.search(r"[\s\\]", raw):
            raise ValueError("invalid_url")
        parsed = urlsplit(raw)
        if (parsed.scheme != "https" or parsed.netloc != "kad.arbitr.ru"
                or parsed.query or parsed.fragment or not re.fullmatch(pattern, parsed.path, re.I)):
            raise ValueError("invalid_url")
        return raw

    card = url(value.get("cardUrl"), rf"/Card/{UUID}")
    card_id = card.rsplit("/", 1)[1]
    pattern = rf"/Kad/PdfDocument/{re.escape(card_id)}/{UUID}/[^/]+\.pdf"
    candidates = value.get("candidates")
    # This automated bridge intentionally admits a sole visible candidate only.
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ValueError("ambiguous_documents")
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise ValueError("invalid_candidate")
    selected = url(value.get("selectedUrl"), pattern)
    if selected != url(candidate.get("url"), pattern):
        raise ValueError("invalid_selection")
    label = candidate.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 2000:
        raise ValueError("invalid_label")
    if value.get("selectionReason") != "only_visible_document":
        raise ValueError("invalid_reason")
    return {"provider": "kad.arbitr.ru", "requestId": run, "caseNumber": number,
            "cardUrl": card, "selectedUrl": selected, "candidates": [{"url": selected, "label": label}],
            "selectionReason": "only_visible_document"}


def source_digest(source: dict) -> str:
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_case(source: dict | None, card) -> None:
    if source is None:
        return
    expected = validate_source(source)["caseNumber"]
    actual = getattr(getattr(card, "case_number", None), "value", None)
    if not actual or normalize_case_number(str(actual)) != expected:
        raise KadDocumentMismatch("Document case does not match the browser request")
