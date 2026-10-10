"""Database CLI migration safety checks."""

from __future__ import annotations

import unittest
from argparse import Namespace
from unittest.mock import patch

from sqlalchemy import create_engine

from app import cli


class DatabaseCliTests(unittest.TestCase):
    def test_legacy_reset_flag_refuses_destructive_schema_drop(self) -> None:
        with patch.object(cli.alembic_command, "upgrade") as upgrade:
            self.assertEqual(cli.cmd_init_db(Namespace(reset=True)), 2)

        upgrade.assert_not_called()

    def test_init_db_applies_alembic_head(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with patch.object(cli, "engine", engine), patch.object(
                cli.alembic_command, "upgrade"
            ) as upgrade:
                self.assertEqual(cli.cmd_init_db(Namespace(reset=False)), 0)

            config, revision = upgrade.call_args.args
            self.assertEqual(config.config_file_name, str(cli.ROOT / "alembic.ini"))
            self.assertEqual(revision, "head")
        finally:
            engine.dispose()

    def test_init_db_refuses_existing_unversioned_schema(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE task (id INTEGER PRIMARY KEY)")

            with patch.object(cli, "engine", engine), patch.object(
                cli.alembic_command, "upgrade"
            ) as upgrade:
                self.assertEqual(cli.cmd_init_db(Namespace(reset=False)), 2)

            upgrade.assert_not_called()
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
