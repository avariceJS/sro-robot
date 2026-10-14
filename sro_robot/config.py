"""Настройки робота. Секреты живут в .env, а не в yaml."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"
ENV_FILE = ROOT / ".env"


def _secure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValueError(f"{path.name} must be a real directory without symlinks") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"{path.name} must be a real directory without symlinks")
            if metadata.st_uid != os.geteuid():
                raise ValueError(f"{path.name} directory must be owned by the service user")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)


def _read_private_env(path: Path) -> str | None:
    if os.name != "posix":
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("Secret environment file must be a regular file without symlinks") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Secret environment file must be a regular file without symlinks")
        if metadata.st_uid != os.geteuid():
            raise ValueError("Secret environment file must be owned by the service user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("Secret environment file must not grant group or other access")
        with os.fdopen(descriptor, "r", encoding="utf-8") as env_file:
            descriptor = -1
            return env_file.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _private_env_items(path: Path = ENV_FILE) -> list[tuple[str, str]]:
    contents = _read_private_env(path)
    if contents is None:
        return []
    items: list[tuple[str, str]] = []
    for line in contents.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        items.append((key.strip(), value.strip().strip('"').strip("'")))
    return items


def load_env(path: Path = ENV_FILE) -> None:
    for key, value in _private_env_items(path):
        # A service-readable application .env may contain runtime secrets, but
        # migration credentials must be passed explicitly to the short-lived
        # administrative process and must never enter web/worker settings.
        if key == "MIGRATION_DATABASE_URL":
            continue
        os.environ.setdefault(key, value)


def private_setting(name: str, path: Path = ENV_FILE) -> str:
    """Return a secret setting without letting an empty process value hide the file.

    A long-running worker may have loaded `.env` before the owner filled a key.
    `setdefault` then keeps that empty string. A non-empty process value still wins.
    """
    current = os.environ.get(name, "").strip()
    if current:
        return current
    for key, value in _private_env_items(path):
        if key == name and value:
            return value
    return ""


@dataclass
class Config:
    base_url: str = "https://sroarbitr.sro-service.ru/default.aspx"
    login: str = ""
    password: str = ""
    headless: bool = False
    slow_mo: int = 120
    timeout_ms: int = 30000
    profile_dir: Path = ROOT / "profile"
    out_dir: Path = ROOT / "out"
    selectors: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> "Config":
        load_env()
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        sro = data.get("sro", {})
        cfg = cls(
            base_url=sro.get("base_url", cls.base_url),
            login=private_setting("SRO_LOGIN"),
            password=private_setting("SRO_PASSWORD"),
            headless=bool(sro.get("headless", False)),
            slow_mo=int(sro.get("slow_mo", 120)),
            timeout_ms=int(sro.get("timeout_ms", 30000)),
            selectors=data.get("selectors", {}) or {},
        )
        _secure_private_directory(cfg.profile_dir)
        _secure_private_directory(cfg.out_dir)
        return cfg
