"""Fail-closed identity and choice checks for an Add-created SRO firm draft."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from .errors import SroError


FIRM_CARD_PATH = "/register/firm_card.aspx"
FIRM_BASELINE_FIELDS = (
    "firm_short_name", "firm_full_name", "firm_previous_name", "firm_inn",
    "firm_comment", "firm_ogrn", "firm_kpp",
)


def firm_record_url(url: str, base_url: str) -> bool:
    """Accept one same-origin, nonempty ID on the exact firm-card path."""
    try:
        target = urlsplit(url)
        base = urlsplit(base_url)
        identity = parse_qs(target.query, keep_blank_values=True)
    except (TypeError, ValueError):
        return False
    return bool(
        (target.scheme, target.netloc) == (base.scheme, base.netloc)
        and target.path.casefold() == FIRM_CARD_PATH
        and set(identity) == {"id"}
        and len(identity["id"]) == 1
        and identity["id"][0].strip()
        and target.query.startswith("id=")
        and "&" not in target.query
        and ";" not in target.query
        and not target.fragment
    )


def read_firm_baseline(client) -> dict[str, str]:
    """Read only the top-level firm identity fields and selected catalogue IDs."""
    baseline = {key: (client.el(key).input_value() or "").strip()
                for key in FIRM_BASELINE_FIELDS}
    for key, prefix in (("firm_opf", "opf"), ("firm_status", "status")):
        select = client.el(key)
        baseline[prefix + "_label"] = (
            select.locator("option:checked").inner_text() or ""
        ).strip()
        baseline[prefix + "_value"] = (select.input_value() or "").strip()
    return baseline


def assert_blank_firm_baseline(baseline: dict[str, str]) -> None:
    """The Add-created record has only the exact observed placeholder defaults."""
    if set(baseline) != set(FIRM_BASELINE_FIELDS) | {
        "opf_label", "opf_value", "status_label", "status_value",
    } or any(not isinstance(value, str) for value in baseline.values()):
        raise SroError("SRO firm baseline is incomplete", code="new_firm_not_blank")
    placeholders = {"", "*новая организация"}
    if any(baseline[key].casefold() not in placeholders
           for key in ("firm_short_name", "firm_full_name")):
        raise SroError("SRO new firm form is not blank", code="new_firm_not_blank")
    if any(baseline[key] for key in FIRM_BASELINE_FIELDS[2:]):
        raise SroError("SRO new firm form is not blank", code="new_firm_not_blank")
    if (baseline["opf_label"] != "ООО" or not baseline["opf_value"]
            or baseline["status_label"] != "Оформление" or not baseline["status_value"]):
        raise SroError("SRO new firm defaults changed", code="new_firm_not_blank")


def capture_add_firm(client, add_return_url: str) -> dict[str, str]:
    """Bind the exact Add return URL, then capture a baseline before filling."""
    current_url = client.page.url
    target = urlsplit(current_url)
    base = urlsplit(client.cfg.base_url)
    if (
        (target.scheme, target.netloc) != (base.scheme, base.netloc)
        or target.path.casefold() != FIRM_CARD_PATH
        or target.fragment
    ):
        raise SroError("SRO did not open the new firm card", code="new_firm_url_invalid")
    if current_url != add_return_url or not firm_record_url(current_url, client.cfg.base_url):
        raise SroError(
            "SRO firm card ID has no matching Add provenance; no fields were changed",
            code="new_firm_identity_unbound",
        )
    client._new_firm_form_url = current_url
    return read_firm_baseline(client)


def select_exact_option(client, key: str, label: str) -> str:
    """Choose an exact label; duplicate labels are safe only if already selected."""
    select = client.el(key)
    select.wait_for(state="visible", timeout=client.cfg.timeout_ms)
    options = select.locator("option")
    matches: list[int] = []
    selected_matches: list[int] = []
    for index in range(options.count()):
        option = options.nth(index)
        if (option.inner_text() or "").strip().casefold() != label.casefold():
            continue
        matches.append(index)
        if option.evaluate("el => el.selected"):
            selected_matches.append(index)
    if len(matches) == 1:
        select.select_option(index=matches[0])
    elif len(matches) > 1 and len(selected_matches) == 1:
        # Two observed ООО options have distinct opaque IDs. Keep the fresh
        # form's selected default; never guess the other catalogue entry.
        pass
    else:
        raise SroError(f"SRO option {label!r} is absent or ambiguous", code="firm_option_ambiguous")
    selected = (select.locator("option:checked").inner_text() or "").strip()
    if selected.casefold() != label.casefold():
        raise SroError("SRO option read-back mismatch", code="firm_readback_mismatch")
    return selected
