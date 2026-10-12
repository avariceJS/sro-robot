"""Validate browser-acquired sources and bind them to one local task occurrence."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from sro_robot.core.validators import normalize_case_number

SOURCE_KEY = "_kad_source_v1"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class KadDocumentMismatch(ValueError):
    """The local parser did not confirm the requested case or act date."""


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
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 100:
        raise ValueError("ambiguous_documents")

    def act_date(raw):
        if raw is None or raw == "":
            return None
        if not isinstance(raw, str) or not re.fullmatch(r"[1-9]\d{3}-\d{2}-\d{2}", raw):
            raise ValueError("invalid_act_date")
        try:
            date.fromisoformat(raw)
        except ValueError:
            raise ValueError("invalid_act_date") from None
        return raw

    requested_date = act_date(value.get("actDate"))
    selected = url(value.get("selectedUrl"), pattern)
    normalized = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("invalid_candidate")
        candidate_url = url(candidate.get("url"), pattern)
        label = candidate.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 2000:
            raise ValueError("invalid_label")
        candidate_date = act_date(candidate.get("actDate"))
        normalized.append({"url": candidate_url, "label": label,
                           **({"actDate": candidate_date} if candidate_date else {})})
    if len({item["url"] for item in normalized}) != len(normalized):
        raise ValueError("invalid_candidates")
    if requested_date:
        if any(not item.get("actDate") for item in normalized):
            raise ValueError("document_date_missing")
        matches = [item for item in normalized if item.get("actDate") == requested_date]
        reason = "unique_act_date"
    else:
        matches = normalized
        reason = "only_visible_document"
    if len(matches) != 1:
        raise ValueError("ambiguous_documents")
    if selected != matches[0]["url"]:
        raise ValueError("invalid_selection")
    if value.get("selectionReason") != reason:
        raise ValueError("invalid_reason")
    return {"provider": "kad.arbitr.ru", "requestId": run, "caseNumber": number,
            "cardUrl": card, "selectedUrl": selected, "candidates": normalized,
            "selectionReason": reason, **({"actDate": requested_date} if requested_date else {})}


def source_digest(source: dict) -> str:
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_case(source: dict | None, card) -> None:
    if source is None:
        return
    validated = validate_source(source)
    expected = validated["caseNumber"]
    actual = getattr(getattr(card, "case_number", None), "value", None)
    if not actual or normalize_case_number(str(actual)) != expected:
        raise KadDocumentMismatch("Document case does not match the browser request")

    requested_date = validated.get("actDate")
    if requested_date:
        actual_date = getattr(getattr(card, "doc_date", None), "value", None)
        try:
            parsed_date = datetime.strptime(str(actual_date), "%d.%m.%Y").date().isoformat()
        except (ValueError, TypeError):
            parsed_date = None
        if parsed_date != requested_date:
            raise KadDocumentMismatch("Document act date does not match the browser request")
