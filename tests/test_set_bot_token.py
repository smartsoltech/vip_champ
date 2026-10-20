"""The token is accepted only through stdin and replaced without exposing its value."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.set_bot_token import set_bot_token

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/set_bot_token.py"
TOKEN = "123456789:" + "x" * 35
POSIX = pytest.mark.skipif(os.name != "posix", reason="Deployment requires POSIX file modes")


def run_helper(path, token):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        input=token,
        capture_output=True,
        timeout=10,
    )


@POSIX
def test_replaces_duplicate_assignments_and_preserves_other_lines(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b"# deployment\r\nDATABASE_URL=postgresql://example\r\n"
        b"export TELEGRAM_API_TOKEN = old\r\nOTHER='keep # spaces'\r\n"
        b" TELEGRAM_API_TOKEN=duplicate\r\n'TELEGRAM_API_TOKEN'=third\r\n"
        b"# TELEGRAM_API_TOKEN=comment\r\nLAST=value"
    )
    env_file.chmod(0o600)

    result = run_helper(env_file, TOKEN.encode() + b"\r\n")

    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert env_file.read_bytes() == (
        b"# deployment\r\nDATABASE_URL=postgresql://example\r\n"
        + f"TELEGRAM_API_TOKEN={TOKEN}\r\n".encode()
        + b"OTHER='keep # spaces'\r\n# TELEGRAM_API_TOKEN=comment\r\nLAST=value"
    )


@POSIX
def test_adds_missing_assignment_after_unterminated_line(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=value")
    env_file.chmod(0o600)

    set_bot_token(env_file, TOKEN.encode())

    assert env_file.read_text() == f"OTHER=value\nTELEGRAM_API_TOKEN={TOKEN}\n"


@pytest.mark.parametrize(
    "raw_token",
    [b"", b"malformed-private-value", b"CHANGE_ME_IN_DRONE_SETTINGS", b"x" * 257,
     TOKEN.encode() + b"\nOTHER=injected"],
)
def test_malformed_input_does_not_change_file_or_echo_value(tmp_path, raw_token):
    env_file = tmp_path / ".env"
    original = b"TELEGRAM_API_TOKEN=\nOTHER=value\n"
    env_file.write_bytes(original)
    env_file.chmod(0o600)

    result = run_helper(env_file, raw_token)

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"Traceback" not in result.stderr
    if raw_token:
        assert raw_token not in result.stderr
    assert TOKEN.encode() not in result.stderr
    assert env_file.read_bytes() == original
    assert list(tmp_path.iterdir()) == [env_file]


@POSIX
def test_readonly_file_is_atomically_replaced_with_mode_0600(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_API_TOKEN=\nOTHER=value\n")
    env_file.chmod(0o400)
    previous_inode = env_file.stat().st_ino

    set_bot_token(env_file, TOKEN.encode())

    assert env_file.stat().st_ino != previous_inode
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert env_file.read_text() == f"TELEGRAM_API_TOKEN={TOKEN}\nOTHER=value\n"
    assert list(tmp_path.iterdir()) == [env_file]


@POSIX
def test_symlink_is_refused_without_modifying_target(tmp_path):
    actual = tmp_path / "actual.env"
    actual.write_text("OTHER=unchanged\n")
    actual.chmod(0o600)
    env_file = tmp_path / ".env"
    env_file.symlink_to(actual)

    result = run_helper(env_file, TOKEN.encode())

    assert result.returncode == 1
    assert TOKEN.encode() not in result.stderr
    assert b"Traceback" not in result.stderr
    assert env_file.is_symlink()
    assert actual.read_text() == "OTHER=unchanged\n"


@POSIX
@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_insecure_permissions_are_refused(tmp_path, mode):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=unchanged\n")
    env_file.chmod(mode)

    result = run_helper(env_file, TOKEN.encode())

    assert result.returncode == 1
    assert env_file.read_text() == "OTHER=unchanged\n"
    assert TOKEN.encode() not in result.stderr


def test_directory_is_refused_without_echoing_token(tmp_path):
    result = run_helper(tmp_path, TOKEN.encode())

    assert result.returncode == 1
    assert TOKEN.encode() not in result.stderr
    assert b"Traceback" not in result.stderr


@POSIX
def test_failed_atomic_replace_preserves_original_and_removes_temporary(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=unchanged\n")
    env_file.chmod(0o600)

    def fail_replace(source, destination):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert destination == env_file
        raise OSError("Simulated storage failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="Simulated storage failure"):
        set_bot_token(env_file, TOKEN.encode())

    assert env_file.read_text() == "OTHER=unchanged\n"
    assert list(tmp_path.iterdir()) == [env_file]
