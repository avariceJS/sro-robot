"""Настройки робота. Секреты живут в .env, а не в yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"
ENV_FILE = ROOT / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
            login=os.environ.get("SRO_LOGIN", ""),
            password=os.environ.get("SRO_PASSWORD", ""),
            headless=bool(sro.get("headless", False)),
            slow_mo=int(sro.get("slow_mo", 120)),
            timeout_ms=int(sro.get("timeout_ms", 30000)),
            selectors=data.get("selectors", {}) or {},
        )
        cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        return cfg
