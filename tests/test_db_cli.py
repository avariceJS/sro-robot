"""Database CLI migration safety checks."""

from __future__ import annotations

import unittest
from argparse import Namespace
from unittest.mock import patch

from app import cli


class DatabaseCliTests(unittest.TestCase):
    def test_legacy_reset_flag_refuses_destructive_schema_drop(self) -> None:
        with patch.object(cli.alembic_command, "upgrade") as upgrade:
            self.assertEqual(cli.cmd_init_db(Namespace(reset=True)), 2)

        upgrade.assert_not_called()

    def test_init_db_applies_alembic_head(self) -> None:
        with patch.object(cli.alembic_command, "upgrade") as upgrade:
            self.assertEqual(cli.cmd_init_db(Namespace(reset=False)), 0)

        config, revision = upgrade.call_args.args
        self.assertEqual(config.config_file_name, str(cli.ROOT / "alembic.ini"))
        self.assertEqual(revision, "head")


if __name__ == "__main__":
    unittest.main()
