from __future__ import annotations

import unittest
from pathlib import Path
import subprocess
import sys
import tempfile


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
        self.assertIn("SRO_UPLOAD_DIR=/tmp/sro-robot-migration-storage", unit)
        self.assertIn("EnvironmentFile=/etc/sro-robot/migration.env", unit)
        self.assertIn("/opt/sro-robot/current/.venv/bin/python -m app.cli init-db", unit)
        self.assertNotIn("ReadWritePaths=/opt/sro-robot/storage/documents", unit)
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
        self.assertIn("sro-robot-preview.service", check_text)

    def test_release_activation_is_explicit_verified_and_rollback_guarded(self) -> None:
        root = Path(__file__).resolve().parents[1]
        deploy = (root / "scripts" / "ops" / "deploy_vps.sh").read_text(encoding="utf-8")
        provision = (root / "scripts" / "ops" / "provision_vps_release.sh").read_text(encoding="utf-8")
        pull = (root / "scripts" / "ops" / "deploy_vps_pull.sh").read_text(encoding="utf-8")
        stage = (root / "scripts" / "ops" / "stage_vps_candidate.sh").read_text(encoding="utf-8")
        activate = (root / "scripts" / "ops" / "activate_vps_release.sh").read_text(encoding="utf-8")
        self.assertIn("annotated tag", deploy)
        self.assertIn("origin/main", deploy)
        self.assertIn("scripts/dev/verify.sh", deploy)
        self.assertIn("StrictHostKeyChecking=yes", deploy)
        self.assertIn("provision_vps_release.sh", deploy)
        self.assertIn("flock -n 9", provision)
        self.assertIn("release_preflight.py", provision)
        self.assertIn("chown -R root:root", provision)
        self.assertIn("chmod 0600", provision)
        self.assertLess(
            provision.rindex("systemctl start --now sro-robot-backup.timer"),
            provision.rindex("/usr/local/sbin/sro-robot-activate-release"),
        )
        self.assertIn("StrictHostKeyChecking=yes", pull)
        self.assertIn("refs/heads/main:refs/remotes/origin/main", pull)
        self.assertIn('cat-file -t "refs/tags/$tag"', pull)
        self.assertIn('main_commit" != "$expected_commit"', pull)
        self.assertIn("provision_vps_release.sh", pull)
        self.assertIn("flock -n 8", pull)
        self.assertLess(pull.index('main_commit" != "$expected_commit"'), pull.index('mkdir -m 0700 "$stage"'))
        self.assertIn("chmod -R a+rX,go-w", pull)
        self.assertNotIn("refs/heads/develop", pull)
        self.assertIn("refs/heads/develop:refs/remotes/origin/develop", stage)
        self.assertIn("candidate-staging", stage)
        self.assertNotIn("provision_vps_release.sh", stage)
        self.assertIn("Queue depth: ocr=0 sro=0 sro_preview=", activate)
        self.assertIn("lease( \\(require reconciliation\\))?", activate)
        self.assertIn('cd "$release"', activate)
        self.assertIn("Release ownership or environment permissions are unsafe", activate)
        self.assertIn("mv -Tf", activate)
        self.assertIn("Release health check failed; pointer restored", activate)
        self.assertIn("sro-robot-backup.service", activate)

    def test_release_identity_accepts_private_key_permissions_and_rejects_unsafe_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        validator = root / "scripts" / "ops" / "check_ssh_identity.py"
        deploy = (root / "scripts" / "ops" / "deploy_vps.sh").read_text(encoding="utf-8")
        self.assertIn("check_ssh_identity.py", deploy)
        self.assertNotIn('[[ ! -x "$ssh_key" ]]', deploy)

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "identity"
            key.write_text("synthetic private key material\n", encoding="utf-8")

            key.chmod(0o600)
            accepted = subprocess.run([sys.executable, str(validator), str(key)], check=False)
            self.assertEqual(accepted.returncode, 0)

            key.chmod(0o640)
            rejected = subprocess.run([sys.executable, str(validator), str(key)], check=False)
            self.assertEqual(rejected.returncode, 2)

            key.chmod(0o600)
            link = Path(directory) / "identity-link"
            link.symlink_to(key)
            rejected_link = subprocess.run(
                [sys.executable, str(validator), str(link)], check=False
            )
            self.assertEqual(rejected_link.returncode, 2)

