"""Verified off-host PostgreSQL backups for a PVE host; stdlib only, no DB credentials.

Run as root: python3 scripts/pve_backup.py --config /etc/vip-champ-backup.json
The same fixed program is supplied to remote Python over SSH stdin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

NAME = re.compile(r"backup_(\d{8}T\d{12}Z)_[0-9a-f]{32}\.dump\Z")
SOURCE_DIRECTORY = "/app/db/backups"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15"]
MIN_FREE_BYTES = 128 * 1024 * 1024


class BackupError(RuntimeError):
    """A safe failure label, deliberately excluding subprocess output and credentials."""


class AlreadyRunning(BackupError):
    pass


def storage_path(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise BackupError("Invalid storage path")
    path = PurePosixPath(value)
    if str(path) != value or value == "/" or any(part in {".", ".."} for part in path.parts):
        raise BackupError("Storage path must be an absolute, normalized directory")
    return value


@dataclass(frozen=True)
class Config:
    vmid: int
    container: str
    target: str
    spool: Path
    backup_timeout_seconds: int = 420
    transfer_timeout_seconds: int = 300

    def __post_init__(self):
        if type(self.vmid) is not int or not 100 <= self.vmid <= 999999999:
            raise BackupError("Invalid VM ID")
        if not isinstance(self.container, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.container):
            raise BackupError("Invalid container name")
        if not isinstance(self.target, str) or ":" not in self.target:
            raise BackupError("Invalid off-host target")
        host, directory = self.target.split(":", 1)
        if not re.fullmatch(r"root@[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
            raise BackupError("Off-host target must specify root@hostname")
        storage_path(directory)
        if not self.spool.is_absolute() or self.spool == Path(self.spool.anchor):
            raise BackupError("Local spool must be an absolute directory below the filesystem root")
        for timeout in (self.backup_timeout_seconds, self.transfer_timeout_seconds):
            if type(timeout) is not int or not 1 <= timeout <= 3600:
                raise BackupError("Command timeouts must be between 1 and 3600 seconds")

    @property
    def host(self):
        return self.target.split(":", 1)[0]

    @property
    def remote_directory(self):
        return self.target.split(":", 1)[1]


def load_config(path: Path) -> Config:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        values["spool"] = Path(storage_path(values["spool"]))
        return Config(**values)
    except (OSError, TypeError, ValueError, KeyError):
        raise BackupError("Invalid backup configuration") from None


def timestamp_from_name(name: str) -> datetime:
    match = NAME.fullmatch(name)
    if match is None:
        raise BackupError("Invalid backup filename")
    try:
        return datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise BackupError("Invalid backup timestamp") from None


def parse_source_output(output: bytes) -> str:
    if len(output) > 4096:
        raise BackupError("Unexpected source backup output")
    try:
        value = output.decode("ascii")
    except UnicodeError:
        raise BackupError("Unexpected source backup output") from None
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    path = PurePosixPath(value)
    if str(path) != value or str(path.parent) != SOURCE_DIRECTORY:
        raise BackupError("Unexpected source backup path")
    timestamp_from_name(path.name)
    return value


def secure_directory(directory: Path, *, create=True):
    if not directory.is_absolute() or ".." in directory.parts or directory == Path(directory.anchor):
        raise BackupError("Unsafe storage directory")
    for parent in reversed((directory, *directory.parents)):
        if parent.is_symlink():
            raise BackupError("Symlink in storage directory path")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.is_dir() or directory.is_symlink():
        raise BackupError("Storage directory is not a regular directory")
    if hasattr(os, "geteuid") and directory.stat().st_uid != os.geteuid():
        raise BackupError("Storage directory is not owned by the current user")
    if create:
        directory.chmod(0o700)


def regular_file(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError("Archive or status path is not a private regular file")
    return info


@contextmanager
def create_private_file(path: Path):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        path.chmod(0o600)
        yield output
        output.flush()
        os.fsync(output.fileno())


@contextmanager
def exclusive_lock(directory: Path):
    import fcntl

    path = directory / ".backup.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        regular_file(path)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning("Backup already running") from None
        yield
    finally:
        os.close(descriptor)


def sync_directory(directory: Path):
    if hasattr(os, "O_DIRECTORY"):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def require_free_space(directory: Path):
    if shutil.disk_usage(directory).free < MIN_FREE_BYTES:
        raise BackupError("Backup storage has less than 128 MiB free")


def fingerprint(path: Path) -> tuple[int, str]:
    before = regular_file(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
            raise BackupError("Archive changed during verification")
        if stream.read(5) != b"PGDMP":
            raise BackupError("Archive is not a PostgreSQL custom-format dump")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    after = regular_file(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    ):
        raise BackupError("Archive changed during verification")
    return size, digest.hexdigest()


def retained_names(names: list[str], now: datetime) -> set[str]:
    ordered = sorted(names, key=lambda name: (timestamp_from_name(name), name), reverse=True)
    retained = set(ordered[:24])
    first_day = now.astimezone(UTC).date() - timedelta(days=29)
    today = now.astimezone(UTC).date()
    days = set()
    for name in ordered:
        day = timestamp_from_name(name).date()
        if first_day <= day <= today and day not in days:
            retained.add(name)
            days.add(day)
    return retained


def prune(directory: Path, now: datetime, *, protect=frozenset()):
    secure_directory(directory, create=False)
    candidates = {}
    for path in directory.iterdir():
        if NAME.fullmatch(path.name) is None:
            continue
        try:
            timestamp_from_name(path.name)
        except BackupError:
            continue
        candidates[path.name] = regular_file(path)
    retained = retained_names(list(candidates), now) | set(protect)
    for name, previous in candidates.items():
        if name in retained:
            continue
        path = directory / name
        current = regular_file(path)
        if (previous.st_dev, previous.st_ino) != (current.st_dev, current.st_ino):
            raise BackupError("Archive changed during retention")
        path.unlink()
    sync_directory(directory)


def prune_orphan_partials(directory: Path, now: datetime, *, protect=frozenset()):
    """Our bounded transfers take at most one hour; partials older than 24h are abandoned."""
    secure_directory(directory, create=False)
    cutoff = now.timestamp() - 24 * 60 * 60
    candidates = []
    for path in directory.iterdir():
        if not path.name.endswith(".partial") or NAME.fullmatch(path.name.removesuffix(".partial")) is None:
            continue
        timestamp_from_name(path.name.removesuffix(".partial"))
        previous = regular_file(path)
        if path.name not in protect and previous.st_mtime < cutoff:
            candidates.append((path, previous))
    for path, previous in candidates:
        current = regular_file(path)
        if (previous.st_dev, previous.st_ino, previous.st_mtime_ns, previous.st_size) != (
            current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size,
        ):
            continue
        path.unlink()
    sync_directory(directory)


def atomic_json(path: Path, values: dict):
    if path.exists() or path.is_symlink():
        regular_file(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        with create_private_file(temporary) as output:
            output.write((json.dumps(values, sort_keys=True) + "\n").encode("utf-8"))
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary.exists():
            regular_file(temporary)
            temporary.unlink()


def command(args: list[str], *, timeout: int, step: str, input_data=None, output=None):
    try:
        result = subprocess.run(
            args, input=input_data, stdout=subprocess.PIPE if output is None else output,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BackupError(f"{step}: command unavailable or timed out") from None
    if result.returncode:
        raise BackupError(f"{step}: command failed (exit {result.returncode})")
    return result.stdout


def remote_operation(mode: str, directory: Path, name: str, expected_digest: str, now: datetime):
    timestamp_from_name(name)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise BackupError("Invalid expected SHA256")
    if mode == "cleanup-source":
        if directory.as_posix() != SOURCE_DIRECTORY:
            raise BackupError("Unexpected source cleanup directory")
        secure_directory(directory, create=False)
        path = directory / name
        if fingerprint(path)[1] != expected_digest:
            raise BackupError("Source cleanup checksum mismatch")
        path.unlink()
        sync_directory(directory)
        return {"removed": name}
    if mode not in {"prepare", "publish"}:
        raise BackupError("Invalid remote operation")
    secure_directory(directory)
    partial = directory / (name + ".partial")
    final = directory / name
    if final.exists() or final.is_symlink():
        raise BackupError("Final archive already exists")
    if mode == "prepare":
        require_free_space(directory)
        with create_private_file(partial):
            pass
        return {"ready": name}
    size, digest = fingerprint(partial)
    if digest != expected_digest:
        raise BackupError("Off-host checksum mismatch")
    partial.chmod(0o600)
    os.replace(partial, final)
    sync_directory(directory)
    prune(directory, now, protect={name})
    prune_orphan_partials(directory, now, protect={name + ".partial"})
    return {"name": name, "sha256": digest, "size": size, "verified": True}


def ssh_operation(config: Config, mode: str, name: str, digest: str, now: datetime):
    remote_argv = ["python3", "-", "--remote", mode, config.remote_directory, name, digest, now.isoformat()]
    output = command(
        ["ssh", "-T", *SSH_OPTIONS, config.host, shlex.join(remote_argv)],
        timeout=config.transfer_timeout_seconds, step=f"off-host {mode}",
        input_data=Path(__file__).read_bytes(),
    )
    return parse_ack(output)


def parse_ack(output: bytes):
    try:
        if len(output) > 4096:
            raise ValueError
        return json.loads(output)
    except (TypeError, ValueError, UnicodeError):
        raise BackupError("Invalid verification acknowledgement") from None


def perform_backup(config: Config, now: datetime):
    require_free_space(config.spool)
    source_prefix = ["pct", "exec", str(config.vmid), "--", "docker", "exec", config.container]
    source_path = parse_source_output(command(
        [*source_prefix, "python", "scripts/backup_database.py"],
        timeout=config.backup_timeout_seconds, step="source backup",
    ))
    name = PurePosixPath(source_path).name
    partial = config.spool / (name + ".partial")
    final = config.spool / name
    if final.exists() or final.is_symlink():
        raise BackupError("Local archive already exists")
    owned_partial = False
    try:
        with create_private_file(partial) as output:
            owned_partial = True
            command([*source_prefix, "cat", "--", source_path], output=output,
                    timeout=config.transfer_timeout_seconds, step="source download")
        size, digest = fingerprint(partial)
        if ssh_operation(config, "prepare", name, digest, now) != {"ready": name}:
            raise BackupError("Invalid off-host preparation acknowledgement")
        command(
            ["scp", "-q", *SSH_OPTIONS, str(partial), f"{config.target}/{name}.partial"],
            timeout=config.transfer_timeout_seconds, step="off-host upload",
        )
        expected = {"name": name, "sha256": digest, "size": size, "verified": True}
        if ssh_operation(config, "publish", name, digest, now) != expected:
            raise BackupError("Off-host verification acknowledgement does not match the archive")
        # A successful SSH exit alone is insufficient: the exact verification ACK is required.
        if fingerprint(partial) != (size, digest):
            raise BackupError("Local archive changed after transfer")
        if final.exists() or final.is_symlink():
            raise BackupError("Local archive appeared during transfer")
        os.replace(partial, final)
        sync_directory(config.spool)
        atomic_json(config.spool / "success.json", {
            **expected, "verified_at": datetime.now(UTC).isoformat(),
            "vmid": config.vmid, "container": config.container, "target": f"{config.target}/{name}",
        })
        cleanup_argv = ["pct", "exec", str(config.vmid), "--", "docker", "exec", "-i", config.container,
                        "python", "-", "--remote", "cleanup-source", SOURCE_DIRECTORY, name, digest, now.isoformat()]
        cleanup = command(cleanup_argv, timeout=config.transfer_timeout_seconds, step="source cleanup",
                          input_data=Path(__file__).read_bytes())
        if parse_ack(cleanup) != {"removed": name}:
            raise BackupError("Invalid source cleanup acknowledgement")
        prune(config.spool, now, protect={name})
        prune_orphan_partials(config.spool, now, protect={name + ".partial"})
        return expected
    finally:
        if owned_partial and (partial.exists() or partial.is_symlink()):
            regular_file(partial)
            partial.unlink()


def run_backup(config: Config, *, now: datetime | None = None):
    secure_directory(config.spool)
    with exclusive_lock(config.spool):
        try:
            return perform_backup(config, now or datetime.now(UTC))
        except Exception as error:
            atomic_json(config.spool / "failure.json", {
                "failed_at": datetime.now(UTC).isoformat(), "error_type": type(error).__name__,
                "stage": str(error) if isinstance(error, BackupError) else "Unexpected backup failure",
            })
            raise


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "--remote":
            if len(argv) != 6:
                raise BackupError("Invalid remote arguments")
            _, mode, directory, name, digest, at = argv
            now = datetime.fromisoformat(at)
            if now.tzinfo is None:
                raise BackupError("Remote timestamp must include a timezone")
            result = remote_operation(mode, Path(directory), name, digest, now)
            print(json.dumps(result, sort_keys=True))
            return 0
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--config", type=Path, default=Path("/etc/vip-champ-backup.json"))
        options = parser.parse_args(argv)
        if os.name != "posix" or os.geteuid() != 0:
            raise BackupError("Run this command as root on the PVE host")
        result = run_backup(load_config(options.config))
        print(f"Verified off-host backup: {result['name']}")
        return 0
    except AlreadyRunning:
        print("Backup already running; skipped")
        return 0
    except Exception as error:
        print(str(error) if isinstance(error, BackupError) else "Backup failed; inspect status and storage", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
