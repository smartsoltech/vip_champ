import json
import logging
import os
import time

import pytest

from config import ConfigurationError, load_settings
from healthcheck import is_healthy, write_heartbeat
from main import RedactingFormatter


def test_cli_does_not_require_telegram_token():
    settings = load_settings(require_token=False, environ={})
    assert settings.telegram_api_token == ""
    assert settings.backup_dir.is_absolute()
    assert settings.startup_timeout_seconds == 600


def test_environment_validates_bootstrap_identity_and_normalizes_postgres():
    settings = load_settings(require_token=False, environ={
        "DATABASE_URL": "postgresql://user:secret@localhost/app",
        "MASTERADMIN_LOGIN": "owner",
        "MASTERADMIN_PASSWORD": "synthetic password",
        "MASTERADMIN_TELEGRAM_IDS": "123",
        "POST_CHAT_IDS": "-100123,@channel_name,-100123",
    })
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.masteradmin_telegram_ids == frozenset({123})
    assert settings.post_chat_ids == (-100123, "@channel_name")
    assert "synthetic password" not in repr(settings)
    assert "user:secret" not in repr(settings)


@pytest.mark.parametrize("values", [
    {"MASTERADMIN_LOGIN": "owner"},
    {"MASTERADMIN_LOGIN": "owner", "MASTERADMIN_PASSWORD": "long enough password"},
    {"MASTERADMIN_LOGIN": "owner", "MASTERADMIN_PASSWORD": "long enough password",
     "MASTERADMIN_TELEGRAM_IDS": "1,2"},
    {"MASTERADMIN_TELEGRAM_IDS": "-1"},
    {"MASTERADMIN_TELEGRAM_IDS": "abc"},
    {"POST_CHAT_IDS": "arbitrary text"},
    {"AUTH_TTL_SECONDS": "0"},
    {"AUTH_MAX_ATTEMPTS": "invalid"},
    {"HEALTH_MAX_AGE_SECONDS": "1"},
    {"BROADCAST_INTERVAL_SECONDS": "nan"},
    {"BROADCAST_INTERVAL_SECONDS": "0"},
    {"DATABASE_URL": "mysql://localhost/app"},
    {"DATABASE_URL": "sqlite:///"},
    {"BACKUP_DIR": ""},
    {"HEARTBEAT_PATH": ""},
    {"STARTUP_TIMEOUT_SECONDS": "invalid"},
    {"STARTUP_TIMEOUT_SECONDS": "59"},
    {"STARTUP_TIMEOUT_SECONDS": "179", "WATCHDOG_TIMEOUT_SECONDS": "180"},
    {"STARTUP_TIMEOUT_SECONDS": "0"},
    {"SHUTDOWN_TIMEOUT_SECONDS": "4"},
    {"WATCHDOG_TIMEOUT_SECONDS": "90", "HEALTH_MAX_AGE_SECONDS": "90"},
])
def test_invalid_configuration_fails_closed(values):
    with pytest.raises(ConfigurationError):
        load_settings(require_token=False, environ=values)


def test_missing_runtime_token_is_rejected():
    with pytest.raises(ConfigurationError, match="TELEGRAM_API_TOKEN"):
        load_settings(environ={})


def test_startup_budget_can_exceed_or_equal_runtime_budget():
    settings = load_settings(require_token=False, environ={
        "STARTUP_TIMEOUT_SECONDS": "900", "WATCHDOG_TIMEOUT_SECONDS": "300",
        "SHUTDOWN_TIMEOUT_SECONDS": "15",
    })
    assert settings.startup_timeout_seconds == 900
    assert settings.watchdog_timeout_seconds == 300
    assert settings.shutdown_timeout_seconds == 15
    equal = load_settings(require_token=False, environ={"STARTUP_TIMEOUT_SECONDS": "180"})
    assert equal.startup_timeout_seconds == equal.watchdog_timeout_seconds


def test_heartbeat_requires_recent_success_from_current_revision(tmp_path):
    path = tmp_path / "health.json"
    assert not is_healthy(path, 90)
    write_heartbeat(path, "new")
    assert is_healthy(path, 90, "new")
    assert not is_healthy(path, 90, "old")
    path.write_text(json.dumps({"timestamp": time.time() - 120, "pid": os.getpid()}))
    assert not is_healthy(path, 90)
    path.write_text("{broken")
    assert not is_healthy(path, 90)


def test_logging_redacts_message_arguments_and_exception():
    secret = "synthetic-private-password"
    formatter = RedactingFormatter((secret,))
    try:
        raise ValueError(secret)
    except ValueError:
        import sys
        record = logging.LogRecord("test", logging.ERROR, "", 1, "Error %s", (secret,), sys.exc_info())
    rendered = formatter.format(record)
    assert secret not in rendered
    assert "[REDACTED]" in rendered
    token = "123456:" + "x" * 32
    record = logging.LogRecord("test", logging.ERROR, "", 1, "url %s", (token,), None)
    assert token not in formatter.format(record)
