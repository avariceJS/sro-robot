"""Guarded nested address editor for owned SRO person and firm cards.

The editor has separate Add and Save postbacks. This module prepares values for
an already qualified editor; opening or saving the editor belongs to the
guarded scenario and client lifecycle.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from ..core.address import split_address
from .errors import SroError
from .firm_form import firm_record_url, select_exact_option


ADDRESS_FIELD_KEYS = {
    "index": "firm_addr_index",
    "area": "firm_addr_area",
    "district": "firm_addr_district",
    "city": "firm_addr_city",
    "street": "firm_addr_street",
    "build": "firm_addr_build",
    "room": "firm_addr_room",
}

PERSON_ADDRESS_FIELD_KEYS = {
    "index": "addr_index",
    "area": "addr_area",
    "district": "addr_district",
    "city": "addr_city",
    "street": "addr_street",
    "build": "addr_build",
    "room": "addr_room",
}


@dataclass(frozen=True)
class _AddressProfile:
    kind: str
    contacts_tab: str
    add: str
    cancel: str
    type_select: str
    fields: dict[str, str]
    country: str
    box: str


_FIRM = _AddressProfile(
    "firm", "firm_contacts_tab", "firm_address_add", "firm_address_cancel",
    "firm_address_type", ADDRESS_FIELD_KEYS, "firm_addr_country", "firm_addr_box",
)
_PERSON = _AddressProfile(
    "person", "person_contacts_tab", "person_address_add", "person_address_cancel",
    "address_kind_select", PERSON_ADDRESS_FIELD_KEYS, "addr_country", "addr_box",
)

ADDRESS_KIND_LABELS = {
    "postal": "Почтовый адрес",
    "registration": "Адрес регистрации",
    "actual": "Фактический адрес",
}

_TYPE_STEMS = {
    "Почтовый адрес": "почтов",
    "Адрес регистрации": "регистрац",
    "Фактический адрес": "фактическ",
}
_GENERIC_ADDRESS_WORDS = {
    "г", "гор", "город", "ул", "улица", "д", "дом", "кв", "квартира",
    "корп", "корпус", "оф", "офис", "обл", "область", "рн", "район",
}


def _code(profile: _AddressProfile, suffix: str) -> str:
    return f"{profile.kind}_address_{suffix}"


def _owned_record_url(url: str, base_url: str, profile: _AddressProfile) -> bool:
    if profile.kind == "firm":
        return firm_record_url(url, base_url)
    try:
        target = urlsplit(url)
        base = urlsplit(base_url)
        identity = parse_qs(target.query, keep_blank_values=True)
    except (TypeError, ValueError):
        return False
    return bool(
        (target.scheme, target.netloc) == (base.scheme, base.netloc)
        and target.path.casefold() == "/register/person_card.aspx"
        and set(identity) == {"id"} and len(identity["id"]) == 1
        and identity["id"][0].strip() and target.query.startswith("id=")
        and "&" not in target.query and ";" not in target.query
        and not target.fragment
    )


def address_field_values(raw: str, *, profile: _AddressProfile = _FIRM) -> dict[str, str]:
    """Map the local splitter; keep the full line if it drops any segment."""
    text = " ".join(raw.split())
    if not text:
        return {}
    parts = split_address(text)
    normalized_values = [re.sub(r"\W+", "", value).casefold() for value in parts.values()]
    segments = [re.sub(r"\W+", "", value).casefold()
                for value in text.split(",") if value.strip()]
    if not parts or any(segment and not any(segment in value for value in normalized_values)
                        for segment in segments):
        parts = {"street": text}
    box = ""
    if "build" in parts:
        build_parts = [part.strip() for part in parts["build"].split(",") if part.strip()]
        boxes = [part for part in build_parts if re.match(r"^корп(?:ус|\.)?\s*\S", part, re.I)]
        if len(boxes) == 1:
            box = boxes[0]
            parts["build"] = ", ".join(part for part in build_parts if part != box)
    result = {profile.fields[key]: value for key, value in parts.items()
              if key in profile.fields and value}
    if box:
        result[profile.box] = box
    return result


def _require_owned_preview(client, profile: _AddressProfile = _FIRM) -> None:
    url = client.page.url
    if (
        not client.prepare_only or not client.owned_add_required
        or client.operator_save_enabled
        or not _owned_record_url(url, client.cfg.base_url, profile)
        or url not in {
            getattr(client, f"_new_{profile.kind}_form_url", None),
            getattr(client, f"_owned_{profile.kind}_preview_url", None),
        }
    ):
        raise SroError("SRO address editor has no owned preview identity", code=_code(profile, "unbound"))


def _require_owned_save(client, profile: _AddressProfile = _FIRM) -> None:
    url = client.page.url
    if (
        not client.prepare_only or not client.owned_add_required
        or not client.operator_save_enabled
        or not _owned_record_url(url, client.cfg.base_url, profile)
        or url != getattr(client, f"_owned_{profile.kind}_form_url", None)
    ):
        raise SroError("SRO address Save has no owned card identity", code=_code(profile, "unbound"))


def _address_rows(client, profile: _AddressProfile = _FIRM):
    client.el(profile.contacts_tab).click()
    client.settle()
    add = client.el(profile.add)
    add.wait_for(state="visible", timeout=client.cfg.timeout_ms)
    return add.locator("xpath=../following-sibling::table[1]//tr")


def read_address_baseline(client, *, profile: _AddressProfile = _FIRM) -> int:
    """Count rows in the exact owned firm card's address list."""
    _require_owned_preview(client, profile)
    return _address_rows(client, profile).count()


def verify_owned_address_blank(client, *, profile: _AddressProfile = _FIRM) -> None:
    """Require zero address rows on an exact owned preview or Save card."""
    if client.operator_save_enabled:
        _require_owned_save(client, profile)
    else:
        _require_owned_preview(client, profile)
    if _address_rows(client, profile).count() != 0:
        raise SroError("Owned SRO card already has an address", code=f"{profile.kind}_address_drift")


def verify_no_address_change(client, baseline: int, *, profile: _AddressProfile = _FIRM) -> None:
    if not isinstance(baseline, int) or baseline != 0:
        raise SroError("SRO address baseline is not empty", code=_code(profile, "drift"))
    if read_address_baseline(client, profile=profile) != baseline:
        raise SroError("SRO address changed after Cancel", code=_code(profile, "drift"))


def _fill_editor(client, raw: str, label: str,
                 profile: _AddressProfile = _FIRM) -> dict[str, str]:
    selected = select_exact_option(client, profile.type_select, label)
    client.settle()
    if selected != label:
        raise SroError("SRO address type changed", code=_code(profile, "readback_mismatch"))
    values = address_field_values(raw, profile=profile)
    # The vendor's new editor contains literal placeholders in these three
    # input values; clear them, including when the source omits that part.
    for key in (*profile.fields.values(), profile.country, profile.box):
        field = client.el(key)
        field.wait_for(state="visible", timeout=client.cfg.timeout_ms)
        field.scroll_into_view_if_needed()
        field.fill(values.get(key, ""))
    for key in (*profile.fields.values(), profile.country, profile.box):
        if (client.el(key).input_value() or "").strip() != values.get(key, ""):
            raise SroError("SRO address field read-back mismatch", code=_code(profile, "readback_mismatch"))
    selected_after = (client.el(profile.type_select).locator("option:checked").inner_text() or "").strip()
    if selected_after != label:
        raise SroError("SRO address type read-back mismatch", code=_code(profile, "readback_mismatch"))
    return values


def _address_label(raw: str, address_kind: str, profile: _AddressProfile = _FIRM) -> str:
    if not raw.strip():
        raise SroError("SRO address is empty", code=_code(profile, "missing"))
    label = ADDRESS_KIND_LABELS.get(address_kind)
    if not label:
        raise SroError("SRO address type must be selected", code=_code(profile, "kind_missing"))
    return label


def preview_address(client, raw: str, address_kind: str, *,
                    profile: _AddressProfile = _FIRM) -> tuple[dict[str, str], str]:
    """Fill and photograph the nested editor, then Cancel without saving it."""
    label = _address_label(raw, address_kind, profile)
    baseline = read_address_baseline(client, profile=profile)
    if baseline != 0:
        raise SroError("Owned SRO card already has an address", code=_code(profile, "drift"))
    getattr(client, f"arm_owned_{profile.kind}_address_add")()
    client.el(profile.add).click()
    client.el(profile.type_select).wait_for(state="visible", timeout=client.cfg.timeout_ms)
    client.settle()
    try:
        _require_owned_preview(client, profile)
        values = _fill_editor(client, raw, label, profile)
        screenshot = str(client.shot(f"{profile.kind}_address_filled"))
    finally:
        _require_owned_preview(client, profile)
        getattr(client, f"arm_owned_{profile.kind}_address_cancel")()
        client.el(profile.cancel).click()
        client.settle()
        verify_no_address_change(client, baseline, profile=profile)
    return {profile.type_select: label, **values}, screenshot


def read_saved_address_receipt(client, *, expected_label: str | None = None,
                               expected_values: dict[str, str] | None = None,
                               profile: _AddressProfile = _FIRM) -> dict[str, str]:
    """Read one displayed child row on the exact owned firm card."""
    _require_owned_save(client, profile)
    rows = _address_rows(client, profile)
    if rows.count() != 1:
        raise SroError("SRO address Save row count is uncertain", code=_code(profile, "save_unknown"))
    rendered = " ".join(rows.first.inner_text().split())
    if not rendered:
        raise SroError("SRO address Save row is empty", code=_code(profile, "save_unknown"))
    if expected_label is not None:
        stem = _TYPE_STEMS.get(expected_label)
        if stem is None or stem not in rendered.casefold():
            raise SroError("SRO address type missing from saved row", code=_code(profile, "save_unknown"))
    if expected_values is not None:
        rendered_tokens = set(re.findall(r"[\w]+", rendered.casefold()))
        for value in expected_values.values():
            tokens = [token for token in re.findall(r"[\w]+", value.casefold())
                      if token not in _GENERIC_ADDRESS_WORDS]
            if tokens and not all(token in rendered_tokens for token in tokens):
                raise SroError("SRO address value missing from saved row", code=_code(profile, "save_unknown"))
    return {"row_count": "1", "row_sha256": hashlib.sha256(rendered.encode()).hexdigest()}


def verify_saved_address(client, receipt: dict[str, str], *,
                         profile: _AddressProfile = _FIRM) -> None:
    """After top-level Save and fresh GET, require the same displayed child row."""
    if (not isinstance(receipt, dict) or set(receipt) != {"row_count", "row_sha256"}
            or receipt["row_count"] != "1"
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["row_sha256"])):
        raise SroError("SRO address receipt is invalid", code=_code(profile, "save_unknown"))
    if read_saved_address_receipt(client, profile=profile) != receipt:
        raise SroError("SRO address changed after Save", code=_code(profile, "save_unknown"))


def save_address(client, raw: str, address_kind: str, *,
                 profile: _AddressProfile = _FIRM) -> dict[str, str]:
    """Use the employee's one-shot decision to persist one nested address."""
    label = _address_label(raw, address_kind, profile)
    _require_owned_save(client, profile)
    if _address_rows(client, profile).count() != 0:
        raise SroError("Owned SRO card already has an address", code=_code(profile, "drift"))
    getattr(client, f"arm_operator_{profile.kind}_address_add")()
    client.el(profile.add).click()
    client.el(profile.type_select).wait_for(state="visible", timeout=client.cfg.timeout_ms)
    client.settle()
    nested_save_started = False
    try:
        _require_owned_save(client, profile)
        _fill_editor(client, raw, label, profile)
        _require_owned_save(client, profile)
        nested_save_started = True
        getattr(client, f"click_operator_{profile.kind}_address_save")()
    finally:
        if not nested_save_started:
            _require_owned_save(client, profile)
            getattr(client, f"arm_operator_{profile.kind}_address_cancel")()
            client.el(profile.cancel).click()
            client.settle()
    return read_saved_address_receipt(
        client, expected_label=label,
        expected_values=address_field_values(raw, profile=profile), profile=profile,
    )


def preview_person_address(client, raw: str, address_kind: str) -> tuple[dict[str, str], str]:
    return preview_address(client, raw, address_kind, profile=_PERSON)


def verify_person_no_address_change(client, baseline: int) -> None:
    verify_no_address_change(client, baseline, profile=_PERSON)


def verify_person_address_blank(client) -> None:
    verify_owned_address_blank(client, profile=_PERSON)


def save_person_address(client, raw: str, address_kind: str) -> dict[str, str]:
    return save_address(client, raw, address_kind, profile=_PERSON)


def verify_saved_person_address(client, receipt: dict[str, str]) -> None:
    verify_saved_address(client, receipt, profile=_PERSON)
