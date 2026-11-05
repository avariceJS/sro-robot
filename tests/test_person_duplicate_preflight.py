"""Read-only duplicate classification before opening a new person form."""

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sro_robot.core.models import DebtorCard, Field
from sro_robot.platform.errors import SroError
from sro_robot.platform.registry import SearchHit
from sro_robot.scenarios.person_card import PersonCardScenario


class PersonDuplicatePreflightTests(unittest.TestCase):
    def check(self, *, inn="500100732259", found_inn="500100732259", href="/register/person_card.aspx?id=synthetic", redirect_url=None):
        client = MagicMock()
        client.cfg = SimpleNamespace(base_url="https://synthetic.invalid/default.aspx", timeout_ms=1000)
        client.page.url = "https://synthetic.invalid/register/person_list.aspx"
        client.el.return_value.input_value.return_value = found_inn
        client.goto.side_effect = lambda url: setattr(client.page, "url", redirect_url or url)
        client.is_logged_in.return_value = True
        registry = MagicMock()
        registry.search.return_value = [SearchHit("Synthetic person", href)]
        card = DebtorCard(case_number=Field("А99-1/2099"), doc_date=Field("01.01.2099"),
                          last_name=Field("Тестов"), first_name=Field("Иван"), inn=Field(inn))
        scenario = PersonCardScenario()
        with patch.object(scenario, "registry", return_value=registry):
            result = scenario.preflight(client, card)
        registry.click_add.assert_not_called()
        client.fill.assert_not_called()
        client.click_close.assert_not_called()
        return result, client, registry, card

    def test_exact_inn_read_from_server_confirms_existing_card(self):
        result, client, registry, _card = self.check()
        self.assertTrue(result.duplicate_identity_confirmed)
        self.assertEqual(len(result.duplicates), 1)
        registry.search.assert_called_once_with("500100732259")
        client.goto.assert_called_once_with("https://synthetic.invalid/register/person_card.aspx?id=synthetic")

    def test_relative_record_link_resolves_from_registry_page(self):
        result, client, _registry, _card = self.check(href="person_card.aspx?id=synthetic")
        self.assertTrue(result.duplicate_identity_confirmed)
        client.goto.assert_called_once_with("https://synthetic.invalid/register/person_card.aspx?id=synthetic")

    def test_multiple_candidates_are_read_until_exact_inn_match(self):
        client = MagicMock()
        client.cfg = SimpleNamespace(base_url="https://synthetic.invalid/default.aspx", timeout_ms=1000)
        client.page.url = "https://synthetic.invalid/register/person_list.aspx"
        client.goto.side_effect = lambda url: setattr(client.page, "url", url)
        client.is_logged_in.return_value = True
        client.el.return_value.input_value.side_effect = ["772881234508", "500100732259"]
        registry = MagicMock()
        registry.search.return_value = [SearchHit("First", "person_card.aspx?id=first"),
                                        SearchHit("Second", "person_card.aspx?id=second")]
        card = DebtorCard(case_number=Field("А99-1/2099"), doc_date=Field("01.01.2099"),
                          last_name=Field("Тестов"), first_name=Field("Иван"), inn=Field("500100732259"))
        scenario = PersonCardScenario()
        with patch.object(scenario, "registry", return_value=registry):
            report = scenario.preflight(client, card)
        self.assertTrue(report.duplicate_identity_confirmed)
        self.assertEqual(report.duplicates, [registry.search.return_value[1]])
        self.assertEqual([call.args[0] for call in client.goto.call_args_list], [
            "https://synthetic.invalid/register/person_card.aspx?id=first",
            "https://synthetic.invalid/register/person_card.aspx?id=second",
        ])
        registry.click_add.assert_not_called()

    def test_search_hit_with_different_inn_is_only_possible_match(self):
        result, _client, _registry, _card = self.check(found_inn="772881234508")
        self.assertFalse(result.duplicate_identity_confirmed)
        self.assertEqual(len(result.duplicates), 1)

    def test_name_only_match_never_asserts_same_identity(self):
        result, client, registry, card = self.check(inn="")
        self.assertFalse(result.duplicate_identity_confirmed)
        registry.search.assert_called_once_with(card.fio)
        client.goto.assert_not_called()

    def test_lookup_cannot_navigate_to_another_origin(self):
        with self.assertRaises(SroError) as caught:
            self.check(href="https://other.invalid/register/person_card.aspx?id=synthetic")
        self.assertEqual(caught.exception.code, "person_lookup_invalid")

    def test_lookup_redirect_to_another_record_does_not_confirm_identity(self):
        with self.assertRaises(SroError) as caught:
            self.check(redirect_url="https://synthetic.invalid/register/person_card.aspx?id=other")
        self.assertEqual(caught.exception.code, "person_lookup_invalid")

    def test_no_inn_results_falls_back_to_possible_name_matches(self):
        client = MagicMock()
        client.cfg = SimpleNamespace(base_url="https://synthetic.invalid/default.aspx", timeout_ms=1000)
        registry = MagicMock()
        registry.search.side_effect = [[], [SearchHit("Synthetic person", "/register/person_card.aspx?id=name")]]
        card = DebtorCard(case_number=Field("А99-1/2099"), doc_date=Field("01.01.2099"),
                          last_name=Field("Тестов"), first_name=Field("Иван"), inn=Field("500100732259"))
        scenario = PersonCardScenario()
        with patch.object(scenario, "registry", return_value=registry):
            report = scenario.preflight(client, card)
        self.assertFalse(report.duplicate_identity_confirmed)
        self.assertEqual(len(report.duplicates), 1)
        self.assertEqual([call.args[0] for call in registry.search.call_args_list], ["500100732259", card.fio])
        client.goto.assert_not_called()
        registry.click_add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
