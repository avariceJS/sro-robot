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
        self.assertIn("User=root", unit)
        self.assertIn("SRO_ROBOT_ENV_FILE=/etc/sro-robot/migration-app.env", unit)
        self.assertIn("EnvironmentFile=/etc/sro-robot/migration.env", unit)
        self.assertIn("/opt/sro-robot/current/.venv/bin/python -m app.cli init-db", unit)
        self.assertNotIn("WantedBy=", unit)

    def test_backup_service_is_private_and_timer_is_persistent(self) -> None:
        service = (UNIT_DIR / "sro-robot-backup.service").read_text(encoding="utf-8")
        timer = (UNIT_DIR / "sro-robot-backup.timer").read_text(encoding="utf-8")
        self.assertIn("User=root", service)
        self.assertIn("EnvironmentFile=/opt/sro-robot/.env", service)
        self.assertIn("UMask=0077", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("/var/backups/sro-robot", service)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=sro-robot-backup.service", timer)

    def test_health_monitor_is_periodic_and_checks_dependencies(self) -> None:
        service = (UNIT_DIR / "sro-robot-health.service").read_text(encoding="utf-8")
        timer = (UNIT_DIR / "sro-robot-health.timer").read_text(encoding="utf-8")
        check = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "check_vps_health.sh"
        check_text = check.read_text(encoding="utf-8")
        self.assertIn("ExecStart=/opt/sro-robot/current/scripts/ops/check_vps_health.sh", service)
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("/readyz", check_text)
        self.assertIn("deploy-db-1 deploy-redis-1", check_text)
        self.assertIn("sro-robot-ocr@1.service", check_text)

    def test_release_activation_is_explicit_verified_and_rollback_guarded(self) -> None:
        root = Path(__file__).resolve().parents[1]
        deploy = (root / "scripts" / "ops" / "deploy_vps.sh").read_text(encoding="utf-8")
        activate = (root / "scripts" / "ops" / "activate_vps_release.sh").read_text(encoding="utf-8")
        self.assertIn("annotated tag", deploy)
        self.assertIn("origin/main", deploy)
        self.assertIn("scripts/dev/verify.sh", deploy)
        self.assertIn("chown -R root:root", deploy)
        self.assertIn("chmod 0600", deploy)
        self.assertLess(
            deploy.rindex("/usr/local/sbin/sro-robot-activate-release"),
            deploy.rindex("systemctl start --now sro-robot-backup.timer"),
        )
        self.assertIn("Queue depth: ocr=0 sro=0 sro_preview=", activate)
        self.assertIn("lease( \\(require reconciliation\\))?", activate)
        self.assertIn('cd "$release"', activate)
        self.assertIn("Release ownership or environment permissions are unsafe", activate)
        self.assertIn("mv -Tf", activate)
        self.assertIn("Release health check failed; pointer restored", activate)
        self.assertIn("sro-robot-backup.service", activate)

