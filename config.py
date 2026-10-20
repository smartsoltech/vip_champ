"""Validated configuration; importing this module has no external side effects."""

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent


class ConfigurationError(ValueError):
    """Invalid configuration; messages never include configured secrets."""


@dataclass(frozen=True)
class Settings:
    telegram_api_token: str = field(default="", repr=False)
    database_url: str = field(
        default=f"sqlite:///{(PROJECT_ROOT / 'db/settings.db').as_posix()}", repr=False,
    )
    masteradmin_login: str = ""
    masteradmin_password: str = field(default="", repr=False)
    masteradmin_telegram_ids: frozenset[int] = frozenset()
    post_chat_ids: tuple[int | str, ...] = ()
    backup_dir: Path = PROJECT_ROOT / "db/backups"
    heartbeat_path: Path = PROJECT_ROOT / "db/heartbeat.json"
    health_max_age_seconds: int = 90
    watchdog_timeout_seconds: int = 180
    startup_timeout_seconds: int = 600
    shutdown_timeout_seconds: int = 30
    auth_ttl_seconds: int = 3600
    state_ttl_seconds: int = 300
    auth_max_attempts: int = 5
    auth_window_seconds: int = 300
    auth_global_max_attempts: int = 50
    broadcast_interval_seconds: float = 0.05
    revision: str = "development"


def _integer(values: Mapping[str, str], key: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(values.get(key, str(default)))
    except (ValueError, TypeError):
        raise ConfigurationError(f"{key} must be an integer") from None
    if value < minimum:
        raise ConfigurationError(f"{key} must be >= {minimum}")
    return value


def _path(values: Mapping[str, str], key: str, default: str) -> Path:
    raw = values.get(key, default).strip()
    if not raw:
        raise ConfigurationError(f"{key} must be a nonempty path")
    value = Path(raw).expanduser()
    return value if value.is_absolute() else PROJECT_ROOT / value


def load_settings(
    require_token: bool = True, *, environ: Mapping[str, str] | None = None
) -> Settings:
    """Load project .env without overriding the process environment."""
    if environ is None:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        environ = os.environ
    token = environ.get("TELEGRAM_API_TOKEN", "").strip()
    if require_token and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        raise ConfigurationError("TELEGRAM_API_TOKEN is missing or malformed")
    database_url = environ.get("DATABASE_URL", Settings().database_url).strip()
    if not database_url.startswith(("sqlite:///", "postgresql://", "postgresql+psycopg://")):
        raise ConfigurationError("DATABASE_URL must use sqlite or PostgreSQL")
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("sqlite:///"):
        sqlite_path = database_url.removeprefix("sqlite:///")
        if not sqlite_path:
            raise ConfigurationError("DATABASE_URL must specify a database")
        if sqlite_path != ":memory:" and not Path(sqlite_path).is_absolute():
            database_url = f"sqlite:///{(PROJECT_ROOT / sqlite_path).as_posix()}"
    try:
        admin_ids = frozenset(
            int(value.strip())
            for value in environ.get("MASTERADMIN_TELEGRAM_IDS", "").split(",")
            if value.strip()
        )
    except ValueError:
        raise ConfigurationError("MASTERADMIN_TELEGRAM_IDS must contain numeric IDs") from None
    if any(value <= 0 for value in admin_ids):
        raise ConfigurationError("MASTERADMIN_TELEGRAM_IDS must contain positive user IDs")
    login = environ.get("MASTERADMIN_LOGIN", "").strip()
    password = environ.get("MASTERADMIN_PASSWORD", "")
    if bool(login) != bool(password):
        raise ConfigurationError("MASTERADMIN_LOGIN and MASTERADMIN_PASSWORD must be set together")
    if login and len(admin_ids) != 1:
        raise ConfigurationError("Bootstrap credentials require exactly one MASTERADMIN_TELEGRAM_IDS ID")
    if password and len(password) < 12:
        raise ConfigurationError("MASTERADMIN_PASSWORD must contain at least 12 characters")
    post_ids: list[int | str] = []
    for value in environ.get("POST_CHAT_IDS", "").split(","):
        value = value.strip()
        if not value:
            continue
        if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value):
            post_ids.append(value)
        elif re.fullmatch(r"-?[1-9]\d*", value):
            post_ids.append(int(value))
        else:
            raise ConfigurationError("POST_CHAT_IDS must contain numeric IDs or @channel names")
    try:
        interval = float(environ.get("BROADCAST_INTERVAL_SECONDS", "0.05"))
    except ValueError:
        raise ConfigurationError("BROADCAST_INTERVAL_SECONDS must be a number") from None
    if not math.isfinite(interval) or interval < 0.04:
        raise ConfigurationError("BROADCAST_INTERVAL_SECONDS must be >= 0.04")
    health_max_age = _integer(environ, "HEALTH_MAX_AGE_SECONDS", 90, 45)
    watchdog_timeout = _integer(environ, "WATCHDOG_TIMEOUT_SECONDS", 180, 60)
    if watchdog_timeout <= health_max_age:
        raise ConfigurationError("WATCHDOG_TIMEOUT_SECONDS must exceed HEALTH_MAX_AGE_SECONDS")
    startup_timeout = _integer(environ, "STARTUP_TIMEOUT_SECONDS", 600, 60)
    if startup_timeout < watchdog_timeout:
        raise ConfigurationError("STARTUP_TIMEOUT_SECONDS must be >= WATCHDOG_TIMEOUT_SECONDS")
    return Settings(
        telegram_api_token=token,
        database_url=database_url,
        masteradmin_login=login,
        masteradmin_password=password,
        masteradmin_telegram_ids=admin_ids,
        post_chat_ids=tuple(dict.fromkeys(post_ids)),
        backup_dir=_path(environ, "BACKUP_DIR", "db/backups"),
        heartbeat_path=_path(environ, "HEARTBEAT_PATH", "db/heartbeat.json"),
        health_max_age_seconds=health_max_age,
        watchdog_timeout_seconds=watchdog_timeout,
        startup_timeout_seconds=startup_timeout,
        shutdown_timeout_seconds=_integer(environ, "SHUTDOWN_TIMEOUT_SECONDS", 30, 5),
        auth_ttl_seconds=_integer(environ, "AUTH_TTL_SECONDS", 3600),
        state_ttl_seconds=_integer(environ, "STATE_TTL_SECONDS", 300),
        auth_max_attempts=_integer(environ, "AUTH_MAX_ATTEMPTS", 5),
        auth_window_seconds=_integer(environ, "AUTH_WINDOW_SECONDS", 300),
        auth_global_max_attempts=_integer(environ, "AUTH_GLOBAL_MAX_ATTEMPTS", 50),
        broadcast_interval_seconds=interval,
        revision=environ.get("APP_REVISION", "development"),
    )
