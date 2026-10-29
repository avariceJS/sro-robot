"""Synthetic test adapter for the one-shot SRO form preparation scenario."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from sro_robot.core.models import DebtorCard, Field
from sro_robot.scenarios.person_card import PersonCardScenario
from sro_robot.platform.errors import SroError


class _Value:
    def __init__(self, adapter: "SyntheticSroFormAdapter", key: str):
        self.adapter = adapter
        self.key = key

    def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        assert timeout == 1000

    def input_value(self) -> str:
        return self.adapter.values.get(self.key, "")

    def locator(self, selector: str) -> "_SelectedOption":
        assert selector == "option:checked"
        return _SelectedOption(self.adapter, self.key)

    def click(self) -> None:
        if self.key == "add_button":
            self.adapter.events.append("add_blank_person")
            self.adapter.page.url = "https://synthetic.invalid/register/person_card.aspx"


class _SelectedOption:
    def __init__(self, adapter: "SyntheticSroFormAdapter", key: str):
        self.adapter = adapter
        self.key = key

    def inner_text(self) -> str:
        return self.adapter.values.get(self.key, "")


class SyntheticSroFormAdapter:
    """Implements only the client surface used by PersonCardScenario.run."""

    dry_run = True
    prepare_only = True

    def __init__(self) -> None:
        self.cfg = SimpleNamespace(timeout_ms=1000, base_url="https://synthetic.invalid/default.aspx")
        self.page = SimpleNamespace(url="https://synthetic.invalid/register/person_list.aspx")
        self.values: dict[str, str] = {}
        self.events: list[str] = []
        self.closed = False

    def ensure_logged_in(self) -> None:
        self.events.append("session_ready")

    def register_selectors(self, _selectors: dict[str, str]) -> None:
        return None

    def goto(self, path: str) -> None:
        self.page.url = f"https://synthetic.invalid{path}"

    def known(self, key: str) -> bool:
        return key == "add_button"

    def require_registry_mutations(self, action: str) -> None:
        assert action == "Add"

    def el(self, key: str) -> _Value:
        return _Value(self, key)

    def settle(self, _pause: float = 0.4) -> None:
        self.events.append("settled")

    def fill(self, key: str, value: str) -> bool:
        self.values[key] = value
        self.events.append(f"fill:{key}")
        return True

    def select_by_text(self, key: str, value: str) -> None:
        self.values[key] = value
        self.events.append(f"select:{key}")

    def shot(self, name: str) -> str:
        return f"synthetic://preview/{name}"

    def click_close(self) -> None:
        self.closed = True
        self.events.append("close_without_save")

    def click_save(self, **_kwargs) -> None:
        raise AssertionError("The synthetic prepare-only adapter forbids Save")


class CloseReviewGate:
    def ask(self, _screenshots: list[str]) -> str:
        return "close"


class SaveReviewGate:
    def __init__(self, change=None):
        self.change = change

    def ask(self, screenshots: list[str]) -> str:
        assert screenshots == ["synthetic://preview/general_filled"]
        if self.change:
            self.change()
        return "save"


class OperatorSaveAdapter(SyntheticSroFormAdapter):
    operator_save_enabled = True

    def __init__(self, saved_url="https://synthetic.invalid/register/person_card.aspx?id=123"):
        super().__init__()
        self.saved_url = saved_url
        self.logged_in = True

    def is_logged_in(self):
        return self.logged_in

    def arm_operator_person_save(self, url):
        assert self.page.url == url
        self.events.append("armed")

    def click_operator_person_save(self):
        assert self.events[-1] == "armed"
        self.events.append("operator_save")
        self.page.url = self.saved_url


class SroPrepareAdapterTests(unittest.TestCase):
    @staticmethod
    def card() -> DebtorCard:
        return DebtorCard(
            case_number=Field("А40-12345/2026", 1.0, "synthetic"),
            doc_date=Field("14.10.2026", 1.0, "synthetic"),
            last_name=Field("Тестов", 1.0, "synthetic"),
            first_name=Field("Иван", 1.0, "synthetic"),
            middle_name=Field("Петрович", 1.0, "synthetic"),
            inn=Field("500100732259", 1.0, "synthetic"),
        )

    def test_default_prepare_only_rejects_save_decision(self):
        adapter = SyntheticSroFormAdapter()
        with self.assertRaisesRegex(SroError, "closing without saving only"):
            PersonCardScenario().run(adapter, self.card(), SaveReviewGate())

    def test_operator_save_requires_identified_record_and_field_readback(self):
        adapter = OperatorSaveAdapter()
        result = PersonCardScenario().run(adapter, self.card(), SaveReviewGate())
        self.assertTrue(result.ok)
        self.assertTrue(result.saved)
        self.assertEqual(result.record_url, adapter.saved_url)
        self.assertEqual(adapter.events.count("operator_save"), 1)
        self.assertFalse(adapter.closed)

    def test_click_without_identified_record_is_unknown_and_never_retried(self):
        for url in (
            "https://synthetic.invalid/register/person_card.aspx",
            "https://synthetic.invalid/register/person_list.aspx",
            "https://synthetic.invalid/register/person_card.aspx?id=",
            "https://synthetic.invalid/register/person_card.aspx?id=1&id=2",
            "https://other.invalid/register/person_card.aspx?id=123",
        ):
            with self.subTest(url=url):
                adapter = OperatorSaveAdapter(saved_url=url)
                with self.assertRaisesRegex(SroError, "неизвестен"):
                    PersonCardScenario().run(adapter, self.card(), SaveReviewGate())
                self.assertEqual(adapter.events.count("operator_save"), 1)
                self.assertFalse(adapter.closed)

    def test_field_url_or_session_drift_after_preview_blocks_browser_save(self):
        for change in (
            lambda a: a.values.update(last_name="Другой"),
            lambda a: a.values.update(status_select="Другой"),
            lambda a: setattr(a.page, "url", a.page.url + "?id=456"),
            lambda a: setattr(a, "logged_in", False),
        ):
            with self.subTest(change=change):
                adapter = OperatorSaveAdapter()
                with self.assertRaises(SroError):
                    PersonCardScenario().run(
                        adapter, self.card(), SaveReviewGate(lambda: change(adapter))
                    )
                self.assertNotIn("operator_save", adapter.events)
                self.assertNotIn("armed", adapter.events)

    def test_no_gate_or_close_never_saves_even_when_operator_save_enabled(self):
        for gate in (None, CloseReviewGate()):
            adapter = OperatorSaveAdapter()
            result = PersonCardScenario().run(adapter, self.card(), gate)
            self.assertTrue(result.ok)
            self.assertFalse(result.saved)
            self.assertTrue(adapter.closed)
            self.assertNotIn("operator_save", adapter.events)

    def test_existing_or_ambiguous_form_url_is_rejected_before_field_changes(self):
        for suffix in ("?id=123", "?ID=123", "?Id=123", "?ID=", "#card"):
            adapter = OperatorSaveAdapter()
            adapter.page.url = "https://synthetic.invalid/register/person_card.aspx" + suffix
            with self.subTest(suffix=suffix), self.assertRaises(SroError):
                PersonCardScenario._require_blank_new_card(adapter)
            self.assertEqual(adapter.values, {})

    def test_synthetic_fields_are_prepared_and_form_is_closed_without_save(self) -> None:
        card = DebtorCard(
            case_number=Field("А40-12345/2026", 1.0, "synthetic"),
            doc_date=Field("14.10.2026", 1.0, "synthetic"),
            last_name=Field("Тестов", 1.0, "synthetic"),
            first_name=Field("Иван", 1.0, "synthetic"),
            middle_name=Field("Петрович", 1.0, "synthetic"),
            inn=Field("500100732259", 1.0, "synthetic"),
        )
        scenario = PersonCardScenario()
        adapter = SyntheticSroFormAdapter()

        result = scenario.run(adapter, card, gate=CloseReviewGate())

        self.assertTrue(result.ok)
        self.assertFalse(result.saved)
        self.assertEqual(result.record_url, "")
        self.assertEqual(
            adapter.values,
            {
                "last_name": "Тестов",
                "first_name": "Иван",
                "middle_name": "Петрович",
                "inn": "500100732259",
                "comment": "А40-12345/2026",
                "sex_select": "M",
                "status_select": "Должник",
            },
        )
        self.assertTrue(adapter.closed)
        self.assertLess(adapter.events.index("add_blank_person"), adapter.events.index("fill:last_name"))
        self.assertEqual(adapter.events[-1], "close_without_save")
        self.assertEqual(result.filled["ИНН"], "500100732259")
        self.assertEqual(result.filled["Комментарий"], "А40-12345/2026")


if __name__ == "__main__":
    unittest.main()
