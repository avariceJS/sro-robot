from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import cli


class CliPasswordSafetyTests(unittest.TestCase):
    def test_create_user_reads_password_from_hidden_prompt(self) -> None:
        session_context = MagicMock()
        session_context.__enter__.return_value = MagicMock()
        created_user = SimpleNamespace(login="synthetic-operator")
        output = StringIO()

        with (
            patch.object(cli, "session_scope", return_value=session_context),
            patch.object(cli.auth, "create_user", return_value=created_user) as create_user,
            patch.object(cli.getpass, "getpass", return_value="synthetic-password") as prompt,
            redirect_stdout(output),
        ):
            result = cli.main(["create-user", "synthetic-operator"])

        self.assertEqual(result, 0)
        prompt.assert_called_once_with("Пароль для входа в приложение: ")
        self.assertEqual(create_user.call_args.args[1:3], ("synthetic-operator", "synthetic-password"))
        self.assertNotIn("synthetic-password", output.getvalue())

    def test_password_argument_forms_are_rejected_without_echoing_value(self) -> None:
        for password_option in (
            ["--password", "synthetic-secret"],
            ["--password=synthetic-secret"],
        ):
            output = StringIO()
            with self.subTest(password_option=password_option), redirect_stdout(output):
                result = cli.main(["create-user", "synthetic-operator", *password_option])

            self.assertEqual(result, 2)
            self.assertIn("Передача пароля в аргументах запрещена", output.getvalue())
            self.assertNotIn("synthetic-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
