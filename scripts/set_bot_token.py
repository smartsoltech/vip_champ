"""Atomically set the deployment token from stdin without logging credentials."""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

MAX_TOKEN_INPUT_BYTES = 256
TOKEN_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?:TELEGRAM_API_TOKEN|'TELEGRAM_API_TOKEN')[ \t]*="
)


def set_bot_token(env_file: Path, raw_token: bytes) -> None:
    if len(raw_token) > MAX_TOKEN_INPUT_BYTES:
        raise ValueError("Telegram token input is too long")
    token_bytes = raw_token.rstrip(b"\r\n")
    if re.fullmatch(rb"[0-9]{5,}:[A-Za-z0-9_-]{20,}", token_bytes) is None:
        raise ValueError("Telegram token is missing or malformed")
    token = token_bytes.decode("ascii")

    metadata = env_file.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Environment path must be a regular file, not a symbolic link")
    if stat.S_IMODE(metadata.st_mode) not in (0o600, 0o400):
        raise ValueError("Environment file must have mode 0600 or 0400")
    descriptor = os.open(env_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("Environment file changed while being opened")
        original = source.read()

    updated = []
    replaced = False
    for line in original.splitlines(keepends=True):
        if TOKEN_ASSIGNMENT.match(line):
            if not replaced:
                ending = "\r\n" if line.endswith("\r\n") else "\n"
                updated.append(f"TELEGRAM_API_TOKEN={token}{ending}")
                replaced = True
        else:
            updated.append(line)
    if not replaced:
        if original and not original.endswith(("\r", "\n")):
            updated.append("\n")
        updated.append(f"TELEGRAM_API_TOKEN={token}\n")

    descriptor, temporary_name = tempfile.mkstemp(prefix=".bot-token-", dir=env_file.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as destination:
            os.fchmod(destination.fileno(), 0o600)
            destination.write("".join(updated))
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, env_file)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: set_bot_token.py ENV_FILE < token", file=sys.stderr)
        return 2
    try:
        raw_token = sys.stdin.buffer.read(MAX_TOKEN_INPUT_BYTES + 1)
        set_bot_token(Path(sys.argv[1]), raw_token)
    except (OSError, UnicodeError):
        print("Unable to update the protected environment file", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
