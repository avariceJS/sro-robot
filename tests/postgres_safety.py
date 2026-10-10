"""Guards for PostgreSQL integration tests that write synthetic rows."""

from __future__ import annotations

import os
import re
import subprocess

from sqlalchemy.engine import make_url


def assert_disposable_postgres() -> None:
    """Require the exact loopback restore-drill container before test writes."""
    if os.environ.get("SRO_DISPOSABLE_DRILL") != "1":
        raise AssertionError("PostgreSQL integration tests require the disposable-drill marker")

    container_name = os.environ.get("SRO_DRILL_CONTAINER_NAME", "")
    if not re.fullmatch(r"sro-restore-drill-[0-9]+", container_name):
        raise AssertionError("PostgreSQL integration tests require a restore-drill container identity")

    try:
        database_url = make_url(os.environ.get("DATABASE_URL", ""))
    except Exception as exc:
        raise AssertionError("PostgreSQL integration test database URL is invalid") from exc
    if (
        database_url.get_backend_name() != "postgresql"
        or database_url.host != "127.0.0.1"
        or database_url.database != "postgres"
        or database_url.port is None
    ):
        raise AssertionError("PostgreSQL integration tests require the restore-drill loopback database")

    try:
        container = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}|{{.Config.Image}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        published = subprocess.run(
            ["docker", "port", container_name, "5432/tcp"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError("Disposable PostgreSQL identity could not be verified") from exc

    endpoints = {line.strip() for line in published.stdout.splitlines()}
    expected_endpoint = f"127.0.0.1:{database_url.port}"
    if (
        container.returncode != 0
        or container.stdout.strip() != "true|postgres:16-alpine"
        or published.returncode != 0
        or expected_endpoint not in endpoints
    ):
        raise AssertionError("Database port is not owned by the disposable restore-drill container")
