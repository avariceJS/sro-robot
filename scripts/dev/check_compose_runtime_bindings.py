#!/usr/bin/env python3
"""Read-only check that running dev containers match Compose port bindings."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.dev.yml"


def _expected_bindings(compose_file: Path) -> dict[str, tuple[str, str, str]]:
    try:
        config = yaml.safe_load(compose_file.read_text())
        services = config["services"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise ValueError("Compose service configuration is unavailable") from exc

    expected: dict[str, tuple[str, str, str]] = {}
    for service_name in ("db", "redis"):
        service = services.get(service_name)
        if not isinstance(service, dict) or not isinstance(service.get("ports"), list):
            raise ValueError(f"Compose port mapping is unavailable for {service_name}")
        mappings = service["ports"]
        if len(mappings) != 1 or not isinstance(mappings[0], str):
            raise ValueError(f"Unsupported Compose port mapping for {service_name}")
        parts = mappings[0].split(":")
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"Unsupported Compose port mapping for {service_name}")
        host_ip, host_port, container_port = parts
        if host_ip != "127.0.0.1":
            raise ValueError(f"Compose port mapping is not loopback-only for {service_name}")
        expected[service_name] = (container_port, host_ip, host_port)
    return expected


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def _inspect_record(line: str) -> dict:
    fields = line.split("\t", 6)
    if len(fields) != 7:
        raise ValueError("Docker inspection returned an unsupported record")
    name, image, running, project, service, config_files, port_bindings = fields
    parsed_bindings = json.loads(port_bindings)
    if parsed_bindings is None:
        parsed_bindings = {}
    if not isinstance(parsed_bindings, dict):
        raise ValueError("Docker inspection returned unsupported port bindings")
    return {
        "name": name.lstrip("/"),
        "image": image,
        "running": running == "true",
        "project": project,
        "service": service,
        "config_files": config_files.split(","),
        "port_bindings": parsed_bindings,
    }


def check_runtime_bindings(compose_file: Path = COMPOSE_FILE) -> tuple[int, list[str]]:
    """Return an exit code and safe, aggregate diagnostics without container IDs."""
    try:
        expected = _expected_bindings(compose_file)
    except ValueError as exc:
        return 2, [str(exc)]

    expected_config = str(compose_file.resolve())
    diagnostics: list[str] = []
    try:
        for service_name, (container_port, expected_ip, expected_host_port) in expected.items():
            compose_command = [
                "docker", "compose", "-f", str(compose_file), "ps", "-q", service_name,
            ]
            listed = _run(compose_command)
            if listed.returncode != 0:
                return 2, [f"Docker Compose inspection failed for service={service_name}"]
            container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
            if len(container_ids) != 1:
                diagnostics.append(
                    f"service={service_name} running_container_count={len(container_ids)} expected=1"
                )
                continue

            template = (
                '{{.Name}}\t{{.Config.Image}}\t{{.State.Running}}\t'
                '{{index .Config.Labels "com.docker.compose.project"}}\t'
                '{{index .Config.Labels "com.docker.compose.service"}}\t'
                '{{index .Config.Labels "com.docker.compose.project.config_files"}}\t'
                '{{json .HostConfig.PortBindings}}'
            )
            inspected = _run(["docker", "inspect", "--format", template, *container_ids])
            if inspected.returncode != 0:
                return 2, [f"Docker container inspection failed for service={service_name}"]
            try:
                records = [_inspect_record(line) for line in inspected.stdout.splitlines() if line]
            except (ValueError, json.JSONDecodeError):
                return 2, [f"Docker returned an unsupported inspection record for service={service_name}"]
            if len(records) != 1:
                diagnostics.append(
                    f"service={service_name} inspected_container_count={len(records)} expected=1"
                )
                continue

            record = records[0]
            target = f"{container_port}/tcp"
            bindings_map = record["port_bindings"]
            bindings = bindings_map.get(target) or []
            actual = {
                (binding.get("HostIp", ""), str(binding.get("HostPort", "")))
                for binding in bindings
            }
            identity_ok = (
                record["running"]
                and record["project"] == "deploy"
                and record["service"] == service_name
                and expected_config in record["config_files"]
            )
            binding_ok = (
                set(bindings_map) == {target}
                and actual == {(expected_ip, expected_host_port)}
            )
            if identity_ok and binding_ok:
                diagnostics.append(f"service={service_name} binding=loopback verified")
            else:
                diagnostics.append(
                    f"service={service_name} binding=mismatch expected={expected_ip}:{expected_host_port}"
                )
    except (OSError, subprocess.TimeoutExpired):
        return 2, ["Docker runtime inspection is unavailable"]

    healthy = all("binding=loopback verified" in item for item in diagnostics)
    return (0 if healthy else 1), diagnostics


def main() -> int:
    code, diagnostics = check_runtime_bindings()
    for item in diagnostics:
        print(item)
    if code == 1:
        print("Runtime containers do not match loopback-only Compose bindings; no service was changed")
    elif code == 2:
        print("Runtime binding check could not complete; no service was changed")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
