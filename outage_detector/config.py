"""Load and validate config.toml.

Secrets (SMTP password etc.) can come from environment variables so the config
file can be committed safely. Any [email] value can be overridden by an env
var: SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_SECURITY,
ALERT_FROM, ALERT_TO (comma separated).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .checks import CHECKS


class ConfigError(Exception):
    pass


@dataclass
class EmailConfig:
    host: str = ""
    port: int = 587
    security: str = "starttls"  # starttls | ssl | none
    username: str = ""
    password: str = ""
    sender: str = ""
    to: list[str] = field(default_factory=list)
    subject_prefix: str = "[Outage Detector]"


@dataclass
class Settings:
    interval_seconds: int = 300
    failure_threshold: int = 2
    recovery_threshold: int = 2
    reminder_minutes: int = 60
    alert_on_degraded: bool = True
    state_file: str = "state.json"
    max_workers: int = 10


@dataclass
class Config:
    settings: Settings
    email: EmailConfig
    services: list[dict]


_ENV_MAP = {
    "host": "SMTP_HOST", "port": "SMTP_PORT", "username": "SMTP_USERNAME",
    "password": "SMTP_PASSWORD", "security": "SMTP_SECURITY",
    "sender": "ALERT_FROM", "to": "ALERT_TO",
}

_REQUIRED = {"http": ["url"], "tcp": ["host", "port"], "statuspage": ["url"],
             "google_workspace": []}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path} "
                          "(copy config.example.toml to config.toml)")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e

    s = raw.get("settings", {})
    unknown = set(s) - set(Settings.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"Unknown [settings] keys: {', '.join(sorted(unknown))}")
    settings = Settings(**s)

    e = dict(raw.get("email", {}))
    if "from" in e:
        e["sender"] = e.pop("from")
    if "password_env" in e:  # e.g. password_env = "GMAIL_APP_PASSWORD"
        e["password"] = os.environ.get(e.pop("password_env"), "")
    for key, env in _ENV_MAP.items():
        if os.environ.get(env):
            e[key] = os.environ[env]
    if isinstance(e.get("to"), str):
        e["to"] = [a.strip() for a in e["to"].split(",") if a.strip()]
    unknown = set(e) - set(EmailConfig.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"Unknown [email] keys: {', '.join(sorted(unknown))}")
    email = EmailConfig(**e)
    email.port = int(email.port)
    email.security = email.security.lower()
    if email.security not in ("starttls", "ssl", "none"):
        raise ConfigError("[email] security must be starttls, ssl or none")
    if not email.sender:
        email.sender = email.username

    services = raw.get("service", [])
    if not services:
        raise ConfigError("No [[service]] entries defined")
    seen: set[str] = set()
    for i, svc in enumerate(services, 1):
        name = svc.get("name")
        if not name:
            raise ConfigError(f"Service #{i} is missing 'name'")
        kind = svc.get("type")
        if kind not in CHECKS:
            raise ConfigError(f"Service '{name}': type must be one of "
                              f"{', '.join(CHECKS)} (got {kind!r})")
        missing = [k for k in _REQUIRED[kind] if k not in svc]
        if missing:
            raise ConfigError(f"Service '{name}' ({kind}) is missing: "
                              f"{', '.join(missing)}")
        svc["id"] = svc.get("id") or _slug(name)
        if svc["id"] in seen:
            raise ConfigError(f"Duplicate service name/id: {svc['id']}")
        seen.add(svc["id"])
        if isinstance(svc.get("notify"), str):
            svc["notify"] = [svc["notify"]]
    return Config(settings, email, services)
