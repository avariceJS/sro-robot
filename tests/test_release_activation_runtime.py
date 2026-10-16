from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "ops" / "activate_vps_release.sh"
COMMIT = "a" * 40
OLD_COMMIT = "b" * 40


class ReleaseActivationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sro-release-activation-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "opt" / "sro-robot"
        self.backups = self.root / "var" / "backups" / "sro-robot"
        self.run_dir = self.root / "run"
        self.maintenance = self.run_dir / "sro-robot-maintenance"
        self.events = self.root / "events.log"
        self.failure_marker = self.root / "systemctl-failure-used"
        self.bin_dir = self.root / "fake-bin"
        self.script = self.root / "activate_vps_release.sh"
        self._create_fake_commands()
        self._create_release(COMMIT)
        self._create_release(OLD_COMMIT)
        (self.base / "current").symlink_to(self.base / "releases" / OLD_COMMIT)
        (self.backups).mkdir(parents=True)
        (self.run_dir / "lock").mkdir(parents=True)
        (self.root / "etc" / "sro-robot").mkdir(parents=True)

        script = SOURCE.read_text(encoding="utf-8")
        replacements = {
            "/opt/sro-robot": str(self.base),
            "/var/backups/sro-robot": str(self.backups),
            "/run": str(self.run_dir),
            "/etc/sro-robot/migration-app.env": str(self.root / "etc" / "sro-robot" / "migration-app.env"),
        }
        for production_path, temporary_path in replacements.items():
            self.assertIn(production_path, script)
            script = script.replace(production_path, temporary_path)
        self.script.write_text(script, encoding="utf-8")
        self.script.chmod(0o755)

    def _write_executable(self, name: str, body: str) -> None:
        path = self.bin_dir / name
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)

    def _create_fake_commands(self) -> None:
        self.bin_dir.mkdir()
        self._write_executable(
            "systemctl",
            """
            #!/bin/sh
            set -eu
            printf 'systemctl %s\\n' "$*" >> "$EVENT_LOG"
            if [ "${SRO_FAIL_SYSTEMCTL:-}" = "$*" ] && [ ! -e "$SYSTEMCTL_FAILURE_MARKER" ]; then
              : > "$SYSTEMCTL_FAILURE_MARKER"
              exit 51
            fi
            exit 0
            """,
        )
        self._write_executable(
            "runuser",
            """
            #!/bin/sh
            set -eu
            printf 'runuser %s\\n' "$*" >> "$EVENT_LOG"
            case " $* " in
              *" ops-status "*)
                if [ "${SRO_FAIL_OPS_STATUS:-0}" = 1 ]; then exit 52; fi
                cat <<'STATUS'
            Recognizing tasks without a lease: 0
            Queue depth: ocr=0 sro=0 sro_preview=0
            Registered RQ workers: ocr=1 sro=0 sro_preview=1
            RQ registry entries (cleanup disabled): ocr=started=0,deferred=0,scheduled=0,failed=0 sro=started=0,deferred=0,scheduled=0,failed=0 sro_preview=started=0,deferred=0,scheduled=0,failed=0
            STATUS
                ;;
              *" schema-check "*) exit 0 ;;
            esac
            exit 0
            """,
        )
        self._write_executable(
            "python3",
            """
            #!/bin/sh
            set -eu
            script="$1"
            action="${2:-}"
            case "${script##*/}" in
              release_database.py) ;;
              *) exit 0 ;;
            esac
            if [ -f "$MAINTENANCE_PATH" ]; then maintenance=yes; else maintenance=no; fi
            printf 'release_database %s maintenance=%s\\n' "$action" "$maintenance" >> "$EVENT_LOG"
            case "$action" in
              snapshot)
                [ "$maintenance" = yes ] || exit 53
                if [ "${SRO_FAIL_SNAPSHOT:-0}" = 1 ]; then exit 54; fi
                ;;
              restore)
                [ "$maintenance" = yes ] || exit 55
                grep -q '^systemctl stop sro-robot-preview.service sro-robot-ocr@1.service sro-robot-web.service$' "$EVENT_LOG" || exit 56
                if [ "${SRO_FAIL_DB_RESTORE:-0}" = 1 ]; then exit 57; fi
                ;;
              *) exit 58 ;;
            esac
            exit 0
            """,
        )
        self._write_executable(
            "stat",
            """
            #!/bin/sh
            set -eu
            case "${2:-}" in
              %u) printf '0\\n' ;;
              %U:%G:%a) printf 'srorobot:srorobot:600\\n' ;;
              *) exit 59 ;;
            esac
            """,
        )
        self._write_executable(
            "install",
            """
            #!/bin/sh
            set -eu
            printf 'install %s\\n' "$*" >> "$EVENT_LOG"
            while [ "$#" -gt 2 ]; do
              case "$1" in
                -o|-g|-m) shift 2 ;;
                *) shift ;;
              esac
            done
            source="$1"
            destination="$2"
            mkdir -p "$(dirname "$destination")"
            if [ "$source" = /dev/null ]; then : > "$destination"; else cp "$source" "$destination"; fi
            """,
        )
        self._write_executable(
            "flock",
            """
            #!/bin/sh
            printf 'flock %s\\n' "$*" >> "$EVENT_LOG"
            exit 0
            """,
        )
        self._write_executable(
            "sha256sum",
            """
            #!/bin/sh
            set -eu
            printf 'synthetic-requirements-hash  %s\\n' "$1"
            """,
        )
        self._write_executable(
            "mv",
            """
            #!/bin/sh
            set -eu
            printf 'mv %s\\n' "$*" >> "$EVENT_LOG"
            if [ "$1" = -Tf ]; then rm -f "$3"; /bin/mv "$2" "$3"; else /bin/mv "$@"; fi
            """,
        )
        self._write_executable(
            "readlink",
            """
            #!/bin/sh
            set -eu
            if [ "$1" = -f ]; then /usr/bin/readlink "$2"; else /usr/bin/readlink "$@"; fi
            """,
        )
        self._write_executable(
            "curl",
            """
            #!/bin/sh
            printf 'curl %s\\n' "$*" >> "$EVENT_LOG"
            exit 0
            """,
        )
        self._write_executable(
            "sleep",
            """
            #!/bin/sh
            exit 0
            """,
        )

    def _create_release(self, commit: str) -> None:
        release = self.base / "releases" / commit
        venv = self.base / "venvs" / commit
        (release / "scripts" / "ops").mkdir(parents=True)
        (release / "scripts" / "ops" / "release_database.py").write_text("# fake target\n")
        (release / ".release-commit").write_text(commit + "\n")
        (release / ".env").write_text("synthetic private app environment\n")
        (release / "requirements.txt").write_text("synthetic==1\n")
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
        (venv / ".requirements-sha256").write_text("synthetic-requirements-hash\n")
        (release / ".venv").symlink_to(venv)

    def _run_activation(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(self.bin_dir) + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
                "EVENT_LOG": str(self.events),
                "MAINTENANCE_PATH": str(self.maintenance),
                "SYSTEMCTL_FAILURE_MARKER": str(self.failure_marker),
            }
        )
        env.update(overrides)
        return subprocess.run(
            [str(self.script), COMMIT],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def _event_text(self) -> str:
        return self.events.read_text(encoding="utf-8") if self.events.exists() else ""

    def _current_release(self) -> str:
        return Path(os.readlink(self.base / "current")).name

    def test_success_activates_candidate_and_clears_maintenance(self) -> None:
        result = self._run_activation()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._current_release(), COMMIT)
        self.assertFalse(self.maintenance.exists())
        events = self._event_text()
        self.assertIn("release_database snapshot maintenance=yes", events)
        self.assertNotIn("release_database restore", events)
        self.assertIn(f"Deployed and ready: {COMMIT}", result.stdout)

    def test_ops_status_failure_refuses_before_maintenance(self) -> None:
        result = self._run_activation(SRO_FAIL_OPS_STATUS="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._current_release(), OLD_COMMIT)
        self.assertFalse(self.maintenance.exists())
        events = self._event_text()
        self.assertNotIn("systemctl stop", events)
        self.assertNotIn("release_database snapshot", events)

    def test_snapshot_failure_keeps_old_pointer_without_database_restore(self) -> None:
        result = self._run_activation(SRO_FAIL_SNAPSHOT="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._current_release(), OLD_COMMIT)
        self.assertFalse(self.maintenance.exists())
        events = self._event_text()
        self.assertIn("release_database snapshot maintenance=yes", events)
        self.assertNotIn("release_database restore", events)

    def test_migration_failure_restores_database_and_old_pointer_under_maintenance(self) -> None:
        result = self._run_activation(SRO_FAIL_SYSTEMCTL="start sro-robot-migrate.service")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._current_release(), OLD_COMMIT)
        self.assertFalse(self.maintenance.exists())
        events = self._event_text()
        self.assertIn("release_database restore maintenance=yes", events)
        self.assertLess(
            events.index("release_database restore maintenance=yes"),
            events.rindex("mv -Tf"),
        )

    def test_final_health_timer_failure_restores_database_and_old_pointer(self) -> None:
        result = self._run_activation(SRO_FAIL_SYSTEMCTL="start sro-robot-health.timer")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._current_release(), OLD_COMMIT)
        self.assertFalse(self.maintenance.exists())
        events = self._event_text()
        self.assertIn("release_database restore maintenance=yes", events)

    def test_database_restore_failure_leaves_maintenance_enabled(self) -> None:
        result = self._run_activation(
            SRO_FAIL_SYSTEMCTL="start sro-robot-migrate.service",
            SRO_FAIL_DB_RESTORE="1",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._current_release(), COMMIT)
        self.assertTrue(self.maintenance.is_file())
        self.assertIn("release_database restore maintenance=yes", self._event_text())


if __name__ == "__main__":
    unittest.main()
