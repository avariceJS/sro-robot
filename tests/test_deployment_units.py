from __future__ import annotations

import unittest
from pathlib import Path


UNIT_DIR = Path(__file__).resolve().parents[1] / "deploy" / "cloud" / "systemd"


class ProductionSystemdUnitTests(unittest.TestCase):
    def test_web_and_ocr_units_use_release_pointer_and_persistent_storage(self) -> None:
        for unit_name in ("sro-robot-web.service", "sro-robot-ocr@.service"):
            with self.subTest(unit=unit_name):
                unit = (UNIT_DIR / unit_name).read_text(encoding="utf-8")
                self.assertIn("User=srorobot", unit)
                self.assertIn("WorkingDirectory=/opt/sro-robot/current", unit)
                self.assertIn("/opt/sro-robot/storage/documents", unit)
                self.assertIn("APP_DEBUG=0", unit)
                self.assertNotIn("MIGRATION_DATABASE_URL", unit)
                self.assertNotIn("EnvironmentFile=/etc/sro-robot/migration.env", unit)

    def test_migration_unit_alone_receives_root_managed_migration_environment(self) -> None:
        unit = (UNIT_DIR / "sro-robot-migrate.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", unit)
        self.assertIn("User=srorobot", unit)
        self.assertIn("EnvironmentFile=/etc/sro-robot/migration.env", unit)
        self.assertIn("/opt/sro-robot/current/.venv/bin/python -m app.cli init-db", unit)
        self.assertNotIn("WantedBy=", unit)

