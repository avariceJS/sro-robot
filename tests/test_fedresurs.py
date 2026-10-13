from __future__ import annotations

import unittest
from types import SimpleNamespace

from sro_robot.fedresurs import (
    FedresursLookupError,
    _reject_access_challenge,
    _require_public_fedresurs_page,
    _require_public_fedresurs_url,
)


class _Body:
    def __init__(self, text: str):
        self.text = text

    def inner_text(self, *, timeout: int) -> str:
        return self.text


class FedresursBoundaryTests(unittest.TestCase):
    def test_only_https_public_fedresurs_hosts_are_accepted(self) -> None:
        for url in (
            "https://fedresurs.ru/legalcases/public-id",
            "https://bankrot.fedresurs.ru/bankruptmessages/public-id",
        ):
            with self.subTest(url=url):
                _require_public_fedresurs_url(url)

        for url in (
            "http://fedresurs.ru/search",
            "https://fedresurs.ru.attacker.example/search",
            "https://attacker.example/?next=https://fedresurs.ru",
            "https://bankrot.fedresurs.ru:444/search",
            "file:///tmp/document.pdf",
        ):
            with self.subTest(url=url), self.assertRaises(FedresursLookupError):
                _require_public_fedresurs_url(url)

    def test_page_origin_is_checked_before_interaction(self) -> None:
        _require_public_fedresurs_page(
            SimpleNamespace(url="https://bankrot.fedresurs.ru/bankrupts")
        )
        with self.assertRaises(FedresursLookupError):
            _require_public_fedresurs_page(
                SimpleNamespace(url="https://attacker.example/bankruptmessages/x")
            )

    def test_access_challenge_fails_closed(self) -> None:
        for notice in (
            "Проверка, что вы не робот",
            "Доступ ограничен",
            "Ошибка 429",
            "CAPTCHA",
        ):
            page = SimpleNamespace(locator=lambda selector: _Body(notice))
            with self.subTest(notice=notice), self.assertRaises(FedresursLookupError):
                _reject_access_challenge(page)

    def test_ordinary_public_page_is_not_treated_as_a_challenge(self) -> None:
        page = SimpleNamespace(
            locator=lambda selector: _Body("Сведения о банкротстве и документы")
        )
        _reject_access_challenge(page)


if __name__ == "__main__":
    unittest.main()
