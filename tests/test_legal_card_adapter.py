"""Synthetic checks of firm Add provenance, preview, and one-shot Save gating."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sro_robot.core.entity_extract import EntityDebtorCard
from sro_robot.core.models import Field
from sro_robot.platform.errors import SroError
from sro_robot.platform.registry import SearchHit
from sro_robot.platform.firm_form import (
    assert_blank_firm_baseline, capture_add_firm, firm_record_url, read_firm_baseline,
    select_exact_option,
)
from sro_robot.scenarios.legal_card import FirmCardScenario


def _inn10(prefix: str) -> str:
    return prefix + str(sum(int(n) * w for n, w in zip(prefix, (2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10)


def _inn12(prefix: str) -> str:
    first = sum(int(n) * w for n, w in zip(prefix, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
    second = sum(int(n) * w for n, w in zip(prefix + str(first), (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
    return prefix + str(first) + str(second)


def _ogrn(prefix: str) -> str:
    divisor = 13 if len(prefix) == 14 else 11
    return prefix + str(int(prefix) % divisor % 10)


class _Option:
    def __init__(self, form, key, index):
        self.form, self.key, self.index = form, key, index

    def inner_text(self):
        return self.form.options[self.key][self.index]

    def evaluate(self, _script):
        return self.form.selected[self.key] == self.index


class _Options:
    def __init__(self, form, key):
        self.form, self.key = form, key

    def count(self):
        return len(self.form.options[self.key])

    def nth(self, index):
        return _Option(self.form, self.key, index)


class _Selected:
    def __init__(self, form, key):
        self.form, self.key = form, key

    def inner_text(self):
        return self.form.options[self.key][self.form.selected[self.key]]


class _Element:
    def __init__(self, form, key):
        self.form, self.key = form, key

    def wait_for(self, **_kwargs):
        pass

    def input_value(self):
        if self.key in self.form.options:
            return f"catalogue-id-{self.form.selected[self.key]}"
        return self.form.values.get(self.key, "")

    def locator(self, selector):
        if selector == "option":
            return _Options(self.form, self.key)
        if selector == "option:checked":
            return _Selected(self.form, self.key)
        raise AssertionError(selector)

    def select_option(self, *, index):
        self.form.selected[self.key] = index
        self.form.events.append(f"select:{self.key}")

    def click(self):
        self.form.events.append(f"click:{self.key}")
        if self.key == "add_button":
            self.form.page.url = self.form.add_url


class _Registry:
    def __init__(self, form):
        self.form = form

    def open(self):
        self.form.page.url = "https://synthetic.invalid/register/firm_list.aspx"
        self.form.events.append("list_open")

    def click_add(self):
        self.form.el("add_button").click()
        return self.form.page.url


class _FirmForm:
    prepare_only = True
    dry_run = True

    def __init__(self, *, add_url="https://synthetic.invalid/register/firm_card.aspx?id=fresh", operator_save_enabled=False):
        self.cfg = SimpleNamespace(base_url="https://synthetic.invalid/default.aspx", timeout_ms=1000)
        self.page = SimpleNamespace(url="https://synthetic.invalid/register/firm_list.aspx")
        self.add_url = add_url
        self.operator_save_enabled = operator_save_enabled
        self.values = {"firm_short_name": "*Новая организация", "firm_full_name": "*Новая организация"}
        self.options = {"firm_opf": ["ООО", "ИП", "ООО", "АО"], "firm_status": ["Оформление", "Должник"]}
        self.selected = {"firm_opf": 0, "firm_status": 0}
        self.events = []
        self._new_firm_form_url = None
        self._owned_firm_form_url = None
        self.persisted_values = dict(self.values)
        self.persisted_selected = dict(self.selected)
        self.saved_values = None
        self.saved_selected = None
        self.add_armed = True

    def ensure_logged_in(self):
        pass

    def register_selectors(self, _selectors):
        pass

    def el(self, key):
        return _Element(self, key)

    def fill(self, key, value):
        self.values[key] = value
        self.events.append(f"fill:{key}")
        return True

    def settle(self, *_args):
        pass

    def shot(self, name):
        self.events.append(f"shot:{name}")
        return f"synthetic://{name}"

    def is_logged_in(self):
        return True

    def click_close(self):
        self.events.append("close")
        self.page.url = "https://synthetic.invalid/register/firm_list.aspx"

    def require_owned_firm_add_armed(self):
        assert self.add_armed

    def bind_owned_firm_form(self, url):
        assert self.page.url == url
        self._owned_firm_form_url = url

    def bind_owned_firm_preview(self, url):
        assert self.page.url == url and not self.operator_save_enabled
        self._owned_firm_form_url = url

    def arm_operator_firm_save(self, url):
        assert self._owned_firm_form_url == url == self.page.url
        self.events.append("arm")

    def click_operator_firm_save(self):
        assert self.events[-1] == "arm"
        self.events.append("save")
        self.saved_values = dict(self.values)
        self.saved_selected = dict(self.selected)
        self.page.url = "https://synthetic.invalid/register/firm_list.aspx"

    def goto(self, path):
        self.events.append("fresh_get")
        self.page.url = path if path.startswith("https://") else "https://synthetic.invalid" + path
        self.values = dict(self.saved_values if self.saved_values is not None else self.persisted_values)
        self.selected = dict(self.saved_selected if self.saved_selected is not None else self.persisted_selected)


class _Decision:
    def __init__(self, decision, callback=None):
        self.decision, self.callback = decision, callback

    def ask(self, screenshots):
        assert screenshots == ["synthetic://firm_general_filled", "synthetic://firm_identifiers_filled"]
        if self.callback:
            self.callback()
        return self.decision


class FirmAdapterTests(unittest.TestCase):
    def setUp(self):
        # Synthetic form semantics are explicit. Production remains held until
        # the observed firm Add count change has been reconciled.
        self.qualification = patch("sro_robot.platform.client.FIRM_PREVIEW_QUALIFIED", True)
        self.qualification.start()

    def tearDown(self):
        self.qualification.stop()

    def card(self, kind="legal_entity"):
        card = EntityDebtorCard(entity_kind=Field(kind, 1, "synthetic"), inn=Field(
            _inn12("3000000000") if kind == "sole_proprietor" else _inn10("100000000"), 1, "synthetic"
        ))
        if kind == "sole_proprietor":
            card.debtor_name = Field("Тестов Иван Петрович", 1, "synthetic")
            card.ogrnip = Field(_ogrn("30000000000000"), 1, "synthetic")
        else:
            card.legal_name = Field("ООО «Пример»", 1, "synthetic")
            card.short_name = Field("ООО «Пример»", 1, "synthetic")
            card.legal_form = Field("ООО", 1, "synthetic")
            card.ogrn = Field(_ogrn("100000000000"), 1, "synthetic")
            card.kpp = Field("100000001", 1, "synthetic")
        return card

    def scenario(self, form):
        scenario = FirmCardScenario()
        scenario.registry = lambda _client: _Registry(form)
        return scenario

    def preview(self, form, card):
        ownership = []
        result = self.scenario(form).run(
            form, card,
            on_add=lambda url, baseline: ownership.append((url, baseline, list(form.events))),
        )
        self.assertEqual(len(ownership), 1)
        self.assertEqual(ownership[0][0], form.add_url)
        self.assertNotIn("fill:firm_inn", ownership[0][2])
        return result, ownership[0][1]

    def test_identified_add_requires_exact_provenance_and_blank_form(self):
        form = _FirmForm()
        form.page.url = form.add_url
        baseline = capture_add_firm(form, form.add_url)
        assert_blank_firm_baseline(baseline)
        self.assertEqual(form._new_firm_form_url, form.add_url)
        with self.assertRaises(SroError) as caught:
            capture_add_firm(form, "https://synthetic.invalid/register/firm_card.aspx?id=other")
        self.assertEqual(caught.exception.code, "new_firm_identity_unbound")
        form.values["firm_inn"] = "unexpected"
        with self.assertRaises(SroError) as caught:
            assert_blank_firm_baseline(capture_add_firm(form, form.add_url))
        self.assertEqual(caught.exception.code, "new_firm_not_blank")
        self.assertFalse(firm_record_url("https://synthetic.invalid/register/firm_card.aspx?id=one&id=two", form.cfg.base_url))

    def test_unqualified_live_firm_add_is_held_before_navigation(self):
        form = _FirmForm()
        with patch("sro_robot.platform.client.FIRM_PREVIEW_QUALIFIED", False):
            with self.assertRaises(SroError) as caught:
                self.scenario(form).run(form, self.card(), on_add=lambda *_: None)
        self.assertEqual(caught.exception.code, "firm_add_unqualified")
        self.assertEqual(form.events, [])

    def test_duplicate_opf_label_uses_only_selected_default(self):
        form = _FirmForm()
        self.assertEqual(select_exact_option(form, "firm_opf", "ООО"), "ООО")
        self.assertNotIn("select:firm_opf", form.events)
        form.selected["firm_opf"] = 1
        with self.assertRaises(SroError) as caught:
            select_exact_option(form, "firm_opf", "ООО")
        self.assertEqual(caught.exception.code, "firm_option_ambiguous")

    def test_legal_preview_closes_without_save(self):
        form = _FirmForm()
        result, baseline = self.preview(form, self.card())
        self.assertTrue(result.ok)
        self.assertFalse(result.saved)
        self.assertEqual(result.record_url, "")
        self.assertIn("close", form.events)
        self.assertNotIn("save", form.events)
        self.assertEqual(result.filled["firm_ogrn"], self.card().ogrn.value)
        self.assertEqual(result.owned_draft_url, form.add_url)
        with patch("sro_robot.scenarios.legal_card.verify_no_address_change") as address_check:
            self.scenario(form).verify_owned_blank_after_close(form, form.add_url, baseline)
        address_check.assert_called_once_with(form, 0)
        self.assertEqual(read_firm_baseline(form), baseline)

    def test_preview_includes_explicit_address_editor_screenshot(self):
        form = _FirmForm()
        card = self.card()
        card.address = Field("Город Пример, ул. Тестовая, д. 7", 1, "synthetic")
        card.address_kind = Field("registration", 1, "synthetic")
        with patch("sro_robot.scenarios.legal_card.preview_address", return_value=(
            {"firm_address_type": "Адрес регистрации", "firm_addr_city": "Город Пример"},
            "synthetic://firm_address_filled",
        )) as address_preview:
            result, _baseline = self.preview(form, card)
        address_preview.assert_called_once_with(form, card.address.value, "registration")
        self.assertEqual(len(result.screenshots), 3)
        self.assertEqual(result.screenshots[-1], "synthetic://firm_address_filled")

    def test_add_with_nonblank_form_never_claims_unowned_guid(self):
        form = _FirmForm()
        form.values["firm_inn"] = "unexpected-existing-data"
        called = []
        with self.assertRaises(SroError) as caught:
            self.scenario(form).run(
                form, self.card(), on_add=lambda *_: called.append(True),
            )
        self.assertEqual(caught.exception.code, "new_firm_not_blank")
        self.assertEqual(called, [])
        self.assertNotIn("fill:firm_inn", form.events)

    def test_close_readback_must_match_original_blank_guid(self):
        form = _FirmForm()
        _result, baseline = self.preview(form, self.card())
        form.persisted_values["firm_inn"] = "unexpected-persisted-data"
        with self.assertRaises(SroError) as caught:
            self.scenario(form).verify_owned_blank_after_close(form, form.add_url, baseline)
        self.assertEqual(caught.exception.code, "firm_close_unverified")

    def test_ip_preview_uses_ip_opf_and_ogrnip(self):
        form = _FirmForm()
        result, _baseline = self.preview(form, self.card("sole_proprietor"))
        self.assertTrue(result.ok)
        self.assertEqual(result.filled["firm_opf"], "ИП")
        self.assertEqual(result.filled["firm_ogrn"], self.card("sole_proprietor").ogrnip.value)
        self.assertNotIn("firm_kpp", result.filled)

    def test_partial_identity_preview_omits_unverified_identifiers(self):
        form = _FirmForm()
        card = self.card()
        card.legal_name = Field("Компания Пример", 1, "synthetic")
        card.legal_form = Field()
        card.inn = Field("123", 0.2, "synthetic")
        card.ogrn = Field("bad OCR", 0.2, "synthetic")
        self.assertEqual(self.scenario(form).validate_prepare(card), [])
        self.assertTrue(self.scenario(form).validate(card))
        result, _baseline = self.preview(form, card)
        self.assertTrue(result.ok)
        self.assertNotIn("firm_inn", result.filled)
        self.assertNotIn("firm_ogrn", result.filled)
        self.assertNotIn("firm_opf", result.filled)

    def test_owned_preview_refresh_reuses_guid_without_add(self):
        first = _FirmForm()
        preview, baseline = self.preview(first, self.card())
        refreshed = _FirmForm()
        result = self.scenario(refreshed).run(
            refreshed, self.card(), gate=None,
            owned_record_url=first.add_url,
            expected_baseline=baseline,
            expected_destination=preview.destination_snapshot,
        )
        self.assertTrue(result.ok and not result.saved)
        self.assertEqual(result.owned_draft_url, first.add_url)
        self.assertNotIn("click:add_button", refreshed.events)
        self.assertIn("close", refreshed.events)

    def test_save_requires_operator_and_unchanged_form(self):
        form = _FirmForm()
        baseline = read_firm_baseline(form)
        with self.assertRaises(SroError) as caught:
            self.scenario(form).run(
                form, self.card(), _Decision("save"),
                owned_record_url=form.add_url, expected_baseline=baseline,
                expected_destination={"opf_label": "ООО", "opf_value": "catalogue-id-0"},
            )
        self.assertEqual(caught.exception.code, "firm_save_not_authorized")
        self.assertNotIn("save", form.events)
        form = _FirmForm(operator_save_enabled=True)
        baseline = read_firm_baseline(form)
        with self.assertRaises(SroError) as caught:
            self.scenario(form).run(
                form, self.card(), _Decision("save"),
                owned_record_url=form.add_url, expected_baseline=baseline,
            )
        self.assertEqual(caught.exception.code, "firm_preview_drift")
        self.assertNotIn("save", form.events)
        form = _FirmForm(operator_save_enabled=True)
        baseline = read_firm_baseline(form)
        with self.assertRaises(SroError) as caught:
            self.scenario(form).run(
                form, self.card("sole_proprietor"),
                _Decision("save", lambda: form.values.__setitem__("firm_inn", "changed")),
                owned_record_url=form.add_url, expected_baseline=baseline,
                expected_destination={"opf_label": "ИП", "opf_value": "catalogue-id-1"},
            )
        self.assertEqual(caught.exception.code, "firm_readback_mismatch")
        self.assertNotIn("save", form.events)

    def test_one_operator_save_fresh_readback(self):
        form = _FirmForm(operator_save_enabled=True)
        baseline = read_firm_baseline(form)
        result = self.scenario(form).run(
            form, self.card("sole_proprietor"), _Decision("save"),
            owned_record_url=form.add_url, expected_baseline=baseline,
            expected_destination={"opf_label": "ИП", "opf_value": "catalogue-id-1"},
        )
        self.assertTrue(result.ok and result.saved)
        self.assertEqual(result.record_url, form.add_url)
        self.assertEqual(form.events.count("save"), 1)
        self.assertEqual(form.events.count("fresh_get"), 2)
        self.assertNotIn("click:add_button", form.events)

    def test_address_persists_before_top_save_and_is_verified_after_fresh_get(self):
        form = _FirmForm(operator_save_enabled=True)
        card = self.card()
        card.address = Field("Синтетический адрес", 1, "synthetic")
        card.address_kind = Field("registration", 1, "synthetic")
        self.assertEqual(self.scenario(form).save_blockers(card), [])
        receipt = {"row_count": "1", "row_sha256": "a" * 64}
        with (patch("sro_robot.scenarios.legal_card.save_address", return_value=receipt) as nested,
              patch("sro_robot.scenarios.legal_card.verify_saved_address") as verified):
            result = self.scenario(form).run(
                form, card, _Decision("save"),
                owned_record_url=form.add_url,
                expected_baseline=read_firm_baseline(form),
                expected_destination={"opf_label": "ООО", "opf_value": "catalogue-id-0"},
            )
        self.assertTrue(result.saved)
        nested.assert_called_once_with(form, card.address.value, "registration")
        verified.assert_called_once_with(form, receipt)
        self.assertLess(form.events.index("click:firm_general_tab"), form.events.index("arm"))

    def test_nested_address_uncertainty_never_attempts_top_save(self):
        form = _FirmForm(operator_save_enabled=True)
        card = self.card()
        card.address = Field("Синтетический адрес", 1, "synthetic")
        card.address_kind = Field("registration", 1, "synthetic")
        with patch("sro_robot.scenarios.legal_card.save_address", side_effect=SroError(
            "synthetic nested Save uncertainty", code="firm_address_save_unknown",
        )):
            with self.assertRaises(SroError) as caught:
                self.scenario(form).run(
                    form, card, _Decision("save"),
                    owned_record_url=form.add_url,
                    expected_baseline=read_firm_baseline(form),
                    expected_destination={"opf_label": "ООО", "opf_value": "catalogue-id-0"},
                )
        self.assertEqual(caught.exception.code, "firm_address_save_unknown")
        self.assertNotIn("arm", form.events)
        self.assertNotIn("save", form.events)

    def test_preflight_skips_only_unchanged_owned_blank_guid(self):
        form = _FirmForm()
        baseline = read_firm_baseline(form)
        scenario = FirmCardScenario()

        class Registry:
            def open(self):
                form.page.url = "https://synthetic.invalid/register/firm_list.aspx"

            def search(self, query):
                return [SearchHit("*Новая организация", form.add_url)] if query == scenario_card.inn.value else []

        scenario_card = self.card()
        scenario.registry = lambda _client: Registry()
        result = scenario.preflight(
            form, scenario_card,
            owned_record_url=form.add_url, expected_baseline=baseline,
        )
        self.assertEqual(result.duplicates, [])
        form.persisted_values["firm_inn"] = "changed"
        with self.assertRaises(SroError) as caught:
            scenario.preflight(
                form, scenario_card,
                owned_record_url=form.add_url, expected_baseline=baseline,
            )
        self.assertEqual(caught.exception.code, "firm_preview_drift")

    def test_duplicate_label_save_requires_exact_previewed_option_value(self):
        preview_form = _FirmForm()
        preview, baseline = self.preview(preview_form, self.card())
        self.assertEqual(preview.destination_snapshot,
                         {"opf_label": "ООО", "opf_value": "catalogue-id-0"})
        drifted = _FirmForm(operator_save_enabled=True)
        drifted.selected["firm_opf"] = 2
        drifted.persisted_selected["firm_opf"] = 2
        with self.assertRaises(SroError) as caught:
            self.scenario(drifted).run(
                drifted, self.card(), _Decision("save"),
                owned_record_url=drifted.add_url, expected_baseline=baseline,
                expected_destination=preview.destination_snapshot,
            )
        self.assertEqual(caught.exception.code, "firm_preview_drift")
        self.assertNotIn("save", drifted.events)
        matching = _FirmForm(operator_save_enabled=True)
        result = self.scenario(matching).run(
            matching, self.card(), _Decision("save"),
            owned_record_url=matching.add_url, expected_baseline=baseline,
            expected_destination=preview.destination_snapshot,
        )
        self.assertTrue(result.saved)


if __name__ == "__main__":
    unittest.main()
