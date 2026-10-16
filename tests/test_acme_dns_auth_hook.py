from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from scripts.ops import acme_dns_auth_hook as hook


class AcmeDnsHookTests(unittest.TestCase):
    def test_rejects_unexpected_domain_before_publication(self) -> None:
        with (
            patch.dict(os.environ, {"CERTBOT_DOMAIN": "other.example", "CERTBOT_VALIDATION": "x"}),
            patch.object(hook, "publish") as publish,
            patch.object(hook, "load_account") as load_account,
        ):
            self.assertEqual(hook.main(), 2)
        load_account.assert_not_called()
        publish.assert_not_called()


    def test_publishes_and_waits_for_exact_txt_value(self) -> None:
        with (
            patch.dict(os.environ, {"CERTBOT_DOMAIN": "sro-office.ru", "CERTBOT_VALIDATION": "synthetic-token"}),
            patch.object(hook, "load_account", return_value={"username": "u", "password": "p"}) as load_account,
            patch.object(hook, "publish") as publish,
            patch.object(hook, "visible", side_effect=[False, True]),
            patch.object(hook.time, "sleep") as sleep,
        ):
            self.assertEqual(hook.main(), 0)
        load_account.assert_called_once_with()
        publish.assert_called_once_with({"username": "u", "password": "p"}, "synthetic-token")
        sleep.assert_called_once_with(hook.PROPAGATION_INTERVAL_SECONDS)


    def test_dns_visibility_requires_exact_txt_answer(self) -> None:
        from io import BytesIO

        exact = b'{"Answer":[{"type":16,"data":"\\"synthetic-token\\""}]}'
        with patch.object(hook, "urlopen", return_value=BytesIO(exact)):
            self.assertTrue(hook.visible("synthetic-token"))

        different = b'{"Answer":[{"type":16,"data":"\\"synthetic-token-extra\\""}]}'
        with patch.object(hook, "urlopen", return_value=BytesIO(different)):
            self.assertFalse(hook.visible("synthetic-token"))
