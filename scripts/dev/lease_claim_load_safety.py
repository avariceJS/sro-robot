"""Fail-closed identity check for synthetic PostgreSQL claim diagnostics."""

from __future__ import annotations

import os
import re
import subprocess
from sqlalchemy.engine import make_url


def verify_disposable_postgres(database_url: str) -> None:
    if os.environ.get("SRO_DISPOSABLE_DRILL") != "1":
        raise SystemExit("refusing synthetic writes without the disposable-drill marker")
    parsed = make_url(database_url)
    drill_container = os.environ.get("SRO_DRILL_CONTAINER_NAME", "")
    if (
        parsed.get_backend_name() != "postgresql"
        or parsed.host != "127.0.0.1"
        or not re.fullmatch(r"sro-restore-drill-[0-9]+", drill_container)
    ):
        raise SystemExit(
            "refusing synthetic writes without a loopback restore-drill container identity"
        )

    try:
        container = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}|{{.Config.Image}}",
                drill_container,
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        published_port = subprocess.run(
            ["docker", "port", drill_container, "5432/tcp"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit(
            "refusing synthetic writes because disposable PostgreSQL identity is unverifiable"
        )

    endpoints = {
        endpoint.strip()
        for endpoint in published_port.stdout.splitlines()
        if published_port.returncode == 0
    }
    if (
        container.returncode != 0
        or container.stdout.strip()
        != "true|postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
        or f"127.0.0.1:{parsed.port}" not in endpoints
    ):
        raise SystemExit(
            "refusing synthetic writes because the loopback port is not owned by the restore drill"
        )
