"""Offline tests of backup transport, retention and publication; never invoke PVE/SSH."""

import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import pve_backup as backup

NOW = datetime(2026, 10, 21, 23, 0, tzinfo=UTC)
PAYLOAD = b"PGDMP" + b"synthetic postgres custom archive" * 20
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def name_at(when=NOW, number=1):
    return f"backup_{when.strftime('%Y%m%dT%H%M%S%fZ')}_{number:032x}.dump"


class Transport:
    def __init__(self, directory):
        self.remote = directory / "offhost"
        self.name = name_at()
        self.payload = PAYLOAD
        self.events = []
        self.fail = None
        self.corrupt = False
        self.bad_ack = False

    def run(self, args, **kwargs):
        assert isinstance(args, list)
        assert kwargs["timeout"] > 0
        assert kwargs.get("shell", False) is False
        if args[0] in {"ssh", "scp"}:
            assert "BatchMode=yes" in args
            assert "StrictHostKeyChecking=yes" in args
            assert "ConnectTimeout=15" in args
        if args[0] == "ssh":
            assert args[-2] == "root@sapsan2"
            remote_args = shlex.split(args[-1])
            assert remote_args[:3] == ["python3", "-", "--remote"]
            stage = remote_args[3]
            assert remote_args[4] == "/var/lib/vz/vip-champ-db-backups"
            assert kwargs["input"] == Path(backup.__file__).read_bytes()
        elif args[0] == "scp":
            stage = "upload"
        elif "cleanup-source" in args:
            stage = "cleanup"
            assert args[:7] == ["pct", "exec", "138", "--", "docker", "exec", "-i"]
            assert backup.SOURCE_DIRECTORY in args
        elif args[-1] == "scripts/backup_database.py":
            stage = "backup"
            assert args == ["pct", "exec", "138", "--", "docker", "exec", "vip_champ-telegram-bot-1",
                            "python", "scripts/backup_database.py"]
        else:
            stage = "download"
            assert args[-3:] == ["cat", "--", f"{backup.SOURCE_DIRECTORY}/{self.name}"]
        self.events.append(stage)
        if self.fail == stage:
            return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"synthetic-secret-never-log")
        if stage == "backup":
            data = f"{backup.SOURCE_DIRECTORY}/{self.name}\n".encode()
        elif stage == "download":
            kwargs["stdout"].write(self.payload)
            data = None
        elif stage in {"prepare", "publish"}:
            result = backup.remote_operation(
                stage, self.remote, remote_args[5], remote_args[6], datetime.fromisoformat(remote_args[7]),
            )
            if stage == "publish" and self.bad_ack:
                result["sha256"] = "0" * 64
            data = json.dumps(result).encode()
        elif stage == "upload":
            assert args[-1] == f"root@sapsan2:/var/lib/vz/vip-champ-db-backups/{self.name}.partial"
            content = Path(args[-2]).read_bytes()
            if self.corrupt:
                content += b"corrupted transfer"
            (self.remote / (self.name + ".partial")).write_bytes(content)
            data = b""
        else:
            assert "publish" in self.events
            assert (self.remote / self.name).read_bytes() == PAYLOAD
            assert kwargs["input"] == Path(backup.__file__).read_bytes()
            data = json.dumps({"removed": self.name}).encode()
        return subprocess.CompletedProcess(args, 0, stdout=data, stderr=b"")


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    config = backup.Config(
        vmid=138, container="vip_champ-telegram-bot-1",
        target="root@sapsan2:/var/lib/vz/vip-champ-db-backups", spool=tmp_path / "spool",
    )
    transport = Transport(tmp_path)
    monkeypatch.setattr(backup.subprocess, "run", transport.run)
    if os.name != "posix":
        monkeypatch.setattr(backup, "exclusive_lock", lambda directory: nullcontext())
    return config, transport


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_source_output_accepts_only_the_exact_native_dump_contract(ending):
    path = f"{backup.SOURCE_DIRECTORY}/{name_at()}"
    assert backup.parse_source_output(path.encode() + ending) == path


@pytest.mark.parametrize("value", [
    "/tmp/backup_20261021T230000000000Z_00000000000000000000000000000001.dump\n",
    f"notice\n{backup.SOURCE_DIRECTORY}/{name_at()}\n",
    f"{backup.SOURCE_DIRECTORY}/{name_at()}\n\n",
    f" {backup.SOURCE_DIRECTORY}/{name_at()}\n",
    f"{backup.SOURCE_DIRECTORY}/../{name_at()}\n",
    f"{backup.SOURCE_DIRECTORY}/{name_at()};whoami\n",
    f"{backup.SOURCE_DIRECTORY}/{name_at().replace('.dump', '.sqlite3')}\n",
    f"{backup.SOURCE_DIRECTORY}/{name_at().replace('20261021', '20261399')}\n",
])
def test_source_output_rejects_unexpected_paths_logs_and_dates(value):
    with pytest.raises(backup.BackupError):
        backup.parse_source_output(value.encode())


@pytest.mark.parametrize("target", [
    "root@sapsan2;whoami:/var/lib/vz/backup", "other@sapsan2:/var/lib/vz/backup",
    "root@sapsan2:/", "root@sapsan2:/var/lib/../backup", "root@sapsan2:/tmp/backup with spaces",
])
def test_configuration_rejects_shell_and_path_ambiguity(tmp_path, target):
    with pytest.raises(backup.BackupError):
        backup.Config(138, "vip_champ-telegram-bot-1", target, tmp_path)


def test_retention_is_union_of_latest_24_and_latest_per_day_within_30_days():
    names = [name_at(NOW - timedelta(days=day, hours=hour), day * 3 + hour + 1)
             for day in range(40) for hour in range(3)]
    kept = backup.retained_names(names, NOW)
    assert set(sorted(names, reverse=True)[:24]) <= kept
    assert len(kept) == 46
    for day in range(30):
        assert name_at(NOW - timedelta(days=day), day * 3 + 1) in kept
    assert not any(backup.timestamp_from_name(name).date() < (NOW - timedelta(days=29)).date() for name in kept)


def test_retention_does_not_delete_unrelated_or_partial_files(tmp_path):
    for index in range(40):
        (tmp_path / name_at(NOW - timedelta(days=index), index + 1)).write_bytes(PAYLOAD)
    protected = ["manual.dump", "success.json", name_at() + ".partial", "backup_invalid.dump"]
    for name in protected:
        (tmp_path / name).write_text("preserve", encoding="utf-8")
    backup.prune(tmp_path, NOW)
    assert all((tmp_path / name).read_text(encoding="utf-8") == "preserve" for name in protected)
    assert len([path for path in tmp_path.iterdir() if backup.NAME.fullmatch(path.name)]) == 30


def test_success_is_recorded_only_after_exact_offhost_ack_and_cleanup_is_scoped(scenario):
    config, transport = scenario
    result = backup.run_backup(config, now=NOW)
    assert transport.events == ["backup", "download", "prepare", "upload", "publish", "cleanup"]
    assert result == {"name": name_at(), "sha256": DIGEST, "size": len(PAYLOAD), "verified": True}
    assert (config.spool / name_at()).read_bytes() == PAYLOAD
    assert (transport.remote / name_at()).read_bytes() == PAYLOAD
    status = json.loads((config.spool / "success.json").read_text())
    assert status["verified"] is True
    assert status["sha256"] == DIGEST
    assert not list(config.spool.glob("*.partial"))
    if os.name == "posix":
        for path in [config.spool / name_at(), config.spool / "success.json", transport.remote / name_at()]:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("failure", ["invalid-magic", "upload", "corrupt", "bad-ack"])
def test_failure_preserves_last_verified_success_and_never_cleans_source(scenario, failure):
    config, transport = scenario
    backup.secure_directory(config.spool)
    previous = b'{"previous_verified_success": true}\n'
    (config.spool / "success.json").write_bytes(previous)
    if failure == "invalid-magic":
        transport.payload = b"not a custom archive"
    elif failure == "corrupt":
        transport.corrupt = True
    elif failure == "bad-ack":
        transport.bad_ack = True
    else:
        transport.fail = failure
    with pytest.raises(backup.BackupError):
        backup.run_backup(config, now=NOW)
    assert (config.spool / "success.json").read_bytes() == previous
    error = (config.spool / "failure.json").read_text()
    assert "synthetic-secret-never-log" not in error
    assert "cleanup" not in transport.events
    assert not (config.spool / name_at()).exists()
    assert not list(config.spool.glob("*.partial"))
    if failure == "corrupt":
        assert not (transport.remote / name_at()).exists()
        assert (transport.remote / (name_at() + ".partial")).exists()


def test_failed_upload_does_not_prune_old_copies_on_either_host(scenario):
    config, transport = scenario
    backup.secure_directory(config.spool)
    backup.secure_directory(transport.remote)
    old_names = [name_at(NOW - timedelta(days=day), day + 2) for day in range(1, 45)]
    for directory in [config.spool, transport.remote]:
        for name in old_names:
            (directory / name).write_bytes(PAYLOAD)
    transport.fail = "upload"
    with pytest.raises(backup.BackupError):
        backup.run_backup(config, now=NOW)
    assert all((directory / name).exists() for directory in [config.spool, transport.remote] for name in old_names)


def test_abandoned_partial_is_pruned_only_after_later_success_and_fresh_partial_survives(scenario):
    config, transport = scenario
    transport.fail = "upload"
    with pytest.raises(backup.BackupError):
        backup.run_backup(config, now=NOW)
    abandoned = transport.remote / (transport.name + ".partial")
    assert abandoned.exists()
    yesterday = (NOW - timedelta(hours=25)).timestamp()
    os.utime(abandoned, (yesterday, yesterday))
    fresh = transport.remote / (name_at(NOW, 3) + ".partial")
    fresh.write_bytes(PAYLOAD)
    os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))
    unrelated = transport.remote / "manual.partial"
    unrelated.write_bytes(b"never delete")
    os.utime(unrelated, (yesterday, yesterday))
    transport.name = name_at(NOW, 2)
    transport.fail = None
    backup.run_backup(config, now=NOW)
    assert not abandoned.exists()
    assert fresh.read_bytes() == PAYLOAD
    assert unrelated.read_bytes() == b"never delete"
    assert (transport.remote / transport.name).read_bytes() == PAYLOAD


def test_low_space_gates_local_source_backup_and_remote_partial_creation(scenario, monkeypatch):
    config, transport = scenario
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda path: SimpleNamespace(free=backup.MIN_FREE_BYTES - 1))
    with pytest.raises(backup.BackupError, match="128 MiB"):
        backup.run_backup(config, now=NOW)
    assert transport.events == []
    assert not (config.spool / "success.json").exists()
    assert (config.spool / "failure.json").exists()
    with pytest.raises(backup.BackupError, match="128 MiB"):
        backup.remote_operation("prepare", transport.remote, name_at(), DIGEST, NOW)
    assert not (transport.remote / (name_at() + ".partial")).exists()


def test_cleanup_failure_records_error_without_invalidating_verified_copy(scenario):
    config, transport = scenario
    transport.fail = "cleanup"
    with pytest.raises(backup.BackupError, match="source cleanup"):
        backup.run_backup(config, now=NOW)
    status = json.loads((config.spool / "success.json").read_text())
    assert status["sha256"] == DIGEST
    assert (transport.remote / name_at()).read_bytes() == PAYLOAD
    assert "source cleanup" in (config.spool / "failure.json").read_text()


def test_preexisting_partial_is_never_deleted_by_another_run(scenario):
    config, _ = scenario
    backup.secure_directory(config.spool)
    partial = config.spool / (name_at() + ".partial")
    partial.write_bytes(b"belongs to another run")
    with pytest.raises(FileExistsError):
        backup.run_backup(config, now=NOW)
    assert partial.read_bytes() == b"belongs to another run"


def test_failed_atomic_publication_keeps_partial_and_does_not_prune(tmp_path, monkeypatch):
    partial = tmp_path / (name_at() + ".partial")
    partial.write_bytes(PAYLOAD)
    other = tmp_path / name_at(NOW - timedelta(days=100), 2)
    other.write_bytes(PAYLOAD)
    monkeypatch.setattr(backup.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("synthetic rename failure")))
    with pytest.raises(OSError):
        backup.remote_operation("publish", tmp_path, name_at(), DIGEST, NOW)
    assert partial.exists()
    assert other.exists()
    assert not (tmp_path / name_at()).exists()


def test_failed_status_replacement_preserves_previous_status(tmp_path, monkeypatch):
    status = tmp_path / "success.json"
    status.write_bytes(b"previous verified status")
    monkeypatch.setattr(backup.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("synthetic rename failure")))
    with pytest.raises(OSError):
        backup.atomic_json(status, {"verified": True})
    assert status.read_bytes() == b"previous verified status"
    assert not list(tmp_path.glob("*.partial"))


def make_symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("This platform does not grant symlink creation")


def test_symlinks_in_directory_archive_or_status_are_rejected(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    linked_directory = tmp_path / "linked"
    make_symlink(linked_directory, directory, directory=True)
    with pytest.raises(backup.BackupError, match="Symlink"):
        backup.secure_directory(linked_directory)
    outside = tmp_path / "outside"
    outside.write_bytes(PAYLOAD)
    archive = directory / name_at()
    make_symlink(archive, outside)
    with pytest.raises(backup.BackupError):
        backup.prune(directory, NOW)
    with pytest.raises(backup.BackupError):
        backup.fingerprint(archive)
    status = directory / "success.json"
    make_symlink(status, outside)
    with pytest.raises(backup.BackupError):
        backup.atomic_json(status, {"verified": True})
    assert outside.read_bytes() == PAYLOAD


def test_newly_verified_copy_survives_retention_despite_source_clock_skew(tmp_path):
    old_name = name_at(NOW - timedelta(days=100), 100)
    (tmp_path / (old_name + ".partial")).write_bytes(PAYLOAD)
    for index in range(40):
        (tmp_path / name_at(NOW - timedelta(hours=index), index + 1)).write_bytes(PAYLOAD)
    result = backup.remote_operation("publish", tmp_path, old_name, DIGEST, NOW)
    assert result["verified"] is True
    assert (tmp_path / old_name).read_bytes() == PAYLOAD


def test_source_cleanup_rejects_every_directory_except_exact_container_backup_path(tmp_path):
    path = tmp_path / name_at()
    path.write_bytes(PAYLOAD)
    with pytest.raises(backup.BackupError, match="source cleanup directory"):
        backup.remote_operation("cleanup-source", tmp_path, name_at(), DIGEST, NOW)
    assert path.read_bytes() == PAYLOAD


def test_timeout_is_bounded_and_does_not_expose_subprocess_output(monkeypatch):
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=b"synthetic-secret-never-log")

    monkeypatch.setattr(backup.subprocess, "run", timeout)
    with pytest.raises(backup.BackupError, match="timed out") as failure:
        backup.command(["ssh", "synthetic-host"], timeout=15, step="test transfer")
    assert "synthetic-secret" not in str(failure.value)


@pytest.mark.skipif(os.name != "posix", reason="PVE uses Linux flock")
def test_flock_prevents_overlapping_runs(tmp_path):
    with backup.exclusive_lock(tmp_path), pytest.raises(backup.AlreadyRunning), backup.exclusive_lock(tmp_path):
        pass


def test_fixed_remote_stdin_program_runs_without_repository_dependencies(tmp_path):
    source = Path(backup.__file__).read_bytes()
    args = [sys.executable, "-", "--remote", "prepare", str(tmp_path), name_at(), DIGEST, NOW.isoformat()]
    prepared = subprocess.run(args, input=source, capture_output=True, timeout=10, check=True)
    assert json.loads(prepared.stdout) == {"ready": name_at()}
    (tmp_path / (name_at() + ".partial")).write_bytes(PAYLOAD)
    args[3] = "publish"
    published = subprocess.run(args, input=source, capture_output=True, timeout=10, check=True)
    assert json.loads(published.stdout)["sha256"] == DIGEST
    assert (tmp_path / name_at()).read_bytes() == PAYLOAD
