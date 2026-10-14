"""Synthetic test adapter for the one-shot SRO form preparation scenario."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from sro_robot.core.models import DebtorCard, Field
from sro_robot.scenarios.person_card import PersonCardScenario


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
        self.cfg = SimpleNamespace(timeout_ms=1000)
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


class SroPrepareAdapterTests(unittest.TestCase):
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
