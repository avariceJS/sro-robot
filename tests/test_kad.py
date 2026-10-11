"""Offline tests for the public judicial-document transport boundary."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from sro_robot.kad import (
    KAD_URL,
    _is_case_card_response,
    _page_warning,
    _pdf_from_response,
)


class MkadTransportTests(unittest.TestCase):
    def test_source_is_the_mobile_kad_entry_point(self) -> None:
        self.assertEqual(KAD_URL, "https://mkad.arbitr.ru/Kad")

    def test_case_card_wait_matches_the_mobile_card_endpoint(self) -> None:
        self.assertTrue(
            _is_case_card_response(MagicMock(url="https://mkad.arbitr.ru/Kad/Card?id=fixture"))
        )
        self.assertFalse(
            _is_case_card_response(MagicMock(url="https://mkad.arbitr.ru/Card/fixture"))
        )

    def test_page_warning_fails_closed_for_empty_and_challenge_pages(self) -> None:
        empty_page = MagicMock()
        empty_page.locator.return_value.inner_text.return_value = "   "
        challenge_page = MagicMock()
        challenge_page.locator.return_value.inner_text.return_value = "Проверка, что вы не робот"

        self.assertIn("пустую страницу", _page_warning(empty_page))
        self.assertIn("проверку пользователя", _page_warning(challenge_page))

    def test_accepts_successful_pdf_response(self) -> None:
        response = MagicMock(status=200)
        response.body.return_value = b"%PDF-1.7\nfixture"

        self.assertEqual(_pdf_from_response(response), b"%PDF-1.7\nfixture")

    def test_rejects_non_pdf_and_http_errors(self) -> None:
        html_response = MagicMock(status=200)
        html_response.body.return_value = b"<html>temporary error</html>"
        error_response = MagicMock(status=503)
        error_response.body.return_value = b"%PDF-1.7\nnot accepted on error status"

        self.assertIsNone(_pdf_from_response(html_response))
        self.assertIsNone(_pdf_from_response(error_response))
        error_response.body.assert_not_called()

    def test_rejects_response_body_read_failure(self) -> None:
        response = MagicMock(status=200)
        response.body.side_effect = OSError("connection closed")

        self.assertIsNone(_pdf_from_response(response))


if __name__ == "__main__":
    unittest.main()
