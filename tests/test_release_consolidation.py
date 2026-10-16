"""Static release-order guards for the production shell entry points.

These checks cover failure-state boundaries that the unit-text smoke test does
not exercise. They do not run SSH, systemd, Docker or a production operation.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ACTIVATE = (ROOT / "scripts/ops/activate_vps_release.sh").read_text(encoding="utf-8")
DEPLOY = (ROOT / "scripts/ops/deploy_vps.sh").read_text(encoding="utf-8")


def section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin + len(start))
    return source[begin:finish]


class ReleaseControlFlowSafetyTests(unittest.TestCase):
    def test_ops_status_failure_blocks_activation(self) -> None:
        preflight = section(ACTIVATE, "# Preflight uses", "# Nginx checks")
        status_call = section(preflight, "-m app.cli ops-status", "grep -Eq")
        self.assertNotIn("|| true", status_call)
        self.assertLess(preflight.index("-m app.cli ops-status"), preflight.index("schema-check"))

    def test_ingress_opens_after_all_fallible_activation_steps(self) -> None:
        success = section(ACTIVATE, 'systemctl start sro-robot-migrate.service', 'echo "Deployed and ready:')
        self.assertLess(success.index("systemctl start sro-robot-health.timer"), success.index('rm -f "$maintenance"'))
        self.assertLess(success.index("systemctl start sro-robot-health.service"), success.index('rm -f "$maintenance"'))
        tail = success.split('rm -f "$maintenance"', 1)[1]
        self.assertNotRegex(tail, r"\b(systemctl|python3|curl|runuser)\b")

    def test_rollback_closes_ingress_before_stopping_writers(self) -> None:
        rollback = section(ACTIVATE, "rollback() {", "trap cleanup EXIT")
        self.assertLess(rollback.index('install -o root -g root -m 0644 /dev/null "$maintenance"'),
                        rollback.index("systemctl stop sro-robot-preview.service"))
        self.assertLess(rollback.index("systemctl stop sro-robot-preview.service"),
                        rollback.index('release_database.py" restore'))
        self.assertLess(rollback.index("if (( old_preview_active ))"),
                        rollback.index('rm -f "$maintenance"'))

    def test_failed_activation_restores_proxy_without_reopening_maintenance(self) -> None:
        proxy = section(DEPLOY, "restore_proxy() {", "trap restore_proxy ERR")
        self.assertRegex(proxy, r"if \[\[ ! -e /run/sro-robot-maintenance \]\]; then")
        self.assertLess(proxy.index("if [[ ! -e /run/sro-robot-maintenance ]]"),
                        proxy.index("nginx -t && systemctl reload nginx"))

    def test_failed_activation_does_not_enable_new_preview_on_reboot(self) -> None:
        call = DEPLOY.index('/usr/local/sbin/sro-robot-activate-release "$commit"')
        before_activation = DEPLOY[:call]
        if re.search(r"systemctl enable[^\n]*sro-robot-preview\.service", before_activation):
            rollback = section(DEPLOY, "restore_proxy() {", "trap restore_proxy ERR")
            self.assertRegex(
                rollback,
                r"systemctl (disable|revert)[^\n]*sro-robot-preview\.service",
                "A failed release must restore the preview unit's previous boot state",
            )
        after_activation = DEPLOY[call:].split("trap - ERR", 1)[0]
        self.assertNotRegex(
            after_activation,
            r"systemctl (enable|start)[^\n]*sro-robot-preview\.service",
            "A post-commit unit failure would return an error after changing live code and schema",
        )


if __name__ == "__main__":
    unittest.main()
