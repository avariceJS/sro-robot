"""Authenticated encryption and identity binding for SRO cookies."""

from __future__ import annotations

import json
import unittest

from app.services.external_session import (
    InvalidExternalSession,
    open_storage_state,
    seal_storage_state,
    validate_storage_state,
)


class ExternalSessionEncryptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = json.dumps(
            {"cookies": [{"name": "synthetic-cookie", "value": "synthetic-secret-value", "domain": "sroarbitr.sro-service.ru"}], "origins": []}
        ).encode()
        self.key = "synthetic-application-key-material-0000000000000000"

    def test_state_is_authenticated_encrypted_and_bound_to_employee_and_login(self) -> None:
        sealed = seal_storage_state(self.state, self.key, 41, 901, "sroarbitr.sro-service.ru")
        self.assertNotIn(b"synthetic-secret-value", sealed)
        restored = open_storage_state(
            sealed, self.key, 41, 901, "sroarbitr.sro-service.ru"
        )
        self.assertEqual(json.loads(restored), json.loads(self.state))
        for user_id, session_id, key in (
            (42, 901, self.key),
            (41, 902, self.key),
            (41, 901, "another-synthetic-application-key-material-0000"),
        ):
            with self.subTest(user_id=user_id, session_id=session_id):
                with self.assertRaises(InvalidExternalSession):
                    open_storage_state(
                        sealed, key, user_id, session_id, "sroarbitr.sro-service.ru"
                    )

    def test_tampering_and_foreign_cookie_domains_are_rejected(self) -> None:
        sealed = bytearray(
            seal_storage_state(self.state, self.key, 41, 901, "sroarbitr.sro-service.ru")
        )
        sealed[-1] ^= 1
        with self.assertRaises(InvalidExternalSession):
            open_storage_state(bytes(sealed), self.key, 41, 901, "sroarbitr.sro-service.ru")
        with self.assertRaises(InvalidExternalSession):
            validate_storage_state(self.state, "example.invalid")

    def test_every_cookie_and_local_storage_origin_is_confined_to_sro(self) -> None:
        states = (
            {
                "cookies": [
                    {"name": "synthetic-cookie", "value": "synthetic", "domain": "sroarbitr.sro-service.ru"},
                    {"name": "foreign-cookie", "value": "synthetic", "domain": "example.invalid"},
                ],
                "origins": [],
            },
            {
                "cookies": [{"name": "synthetic-cookie", "value": "synthetic", "domain": "sroarbitr.sro-service.ru"}],
                "origins": [{"origin": "https://example.invalid", "localStorage": []}],
            },
            {
                "cookies": [{"name": "synthetic-cookie", "value": "synthetic", "domain": "sroarbitr.sro-service.ru"}],
                "origins": [{"origin": None, "localStorage": []}],
            },
        )
        for value in states:
            with self.subTest(value=value):
                with self.assertRaises(InvalidExternalSession):
                    validate_storage_state(
                        json.dumps(value).encode(), "sroarbitr.sro-service.ru"
                    )

    def test_unknown_storage_state_fields_are_rejected(self) -> None:
        value = json.loads(self.state)
        value["synthetic-extra"] = "synthetic"
        with self.assertRaises(InvalidExternalSession):
            validate_storage_state(
                json.dumps(value).encode(), "sroarbitr.sro-service.ru"
            )

    def test_malformed_and_oversized_state_is_rejected(self) -> None:
        for state in (b"not-json", b"{}", b"{" + b" " * 1_000_000):
            with self.subTest(size=len(state)):
                with self.assertRaises(InvalidExternalSession):
                    validate_storage_state(state, "sroarbitr.sro-service.ru")


if __name__ == "__main__":
    unittest.main()
