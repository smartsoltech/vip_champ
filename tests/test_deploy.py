"""Exercise release/rollback gates with a fake Docker executable, never a daemon."""

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not shutil.which("flock"),
    reason="Deployment shell requires Linux utilities",
)
REVISION = "a" * 40
PREVIOUS = "b" * 40
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"
TOKEN_WRITER = SCRIPT.with_name("set_bot_token.py")

DOCKER = """#!/usr/bin/env python3
import os
from pathlib import Path
import sys
args = sys.argv[1:]
state = Path(os.environ['MOCK_STATE'])
with Path(os.environ['MOCK_LOG']).open('a') as log:
    log.write(' '.join(args) + '\\n')
if args[0] == 'compose':
    if 'run' in args:
        if '-c' in args and args[-1] == 'from config import load_settings; load_settings()':
            raise SystemExit(8 if os.environ.get('MOCK_CONFIG_FAIL') else 0)
        if args[-1] == 'scripts/backup_database.py':
            raise SystemExit(7 if os.environ.get('MOCK_BACKUP_FAIL') else 0)
        raise SystemExit(9)
    if 'up' in args:
        state.write_text(os.environ['APP_REVISION'])
    if 'ps' in args:
        print('fake-container')
elif args[:2] == ['image', 'inspect']:
    print(os.environ['APP_REVISION'])
elif args[0] == 'inspect':
    revision = state.read_text()
    template = args[-1]
    if 'State.Status' in template:
        print('running')
    elif 'State.Health' in template:
        unhealthy = revision == os.environ.get('MOCK_UNHEALTHY_REVISION') or os.environ.get('MOCK_ALL_UNHEALTHY')
        print('unhealthy' if unhealthy else 'healthy')
    else:
        print(revision)
"""


def run_deploy(tmp_path, *, previous=False, config_fail=False, backup_fail=False, unhealthy=False,
               rollback_fail=False, symlink=False):
    root = tmp_path / "application"
    (root / "shared/db/backups").mkdir(parents=True)
    env_file = root / "shared/.env"
    env_file.write_text("# External configuration is not inspected by this mock.\n")
    env_file.chmod(0o600)
    old_release = root / "releases/previous"
    if previous:
        old_release.mkdir(parents=True)
        (old_release / "REVISION").write_text(PREVIOUS)
        (old_release / "docker-compose.yml").write_text("services: {}\n")
        (root / "current").symlink_to(old_release)
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        content = b"services: {}\n"
        info = tarfile.TarInfo("docker-compose.yml")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
        tar.add(TOKEN_WRITER, arcname="scripts/set_bot_token.py")
        if symlink:
            info = tarfile.TarInfo("escape")
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp"
            tar.addfile(info)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text(DOCKER)
    (binaries / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for binary in binaries.iterdir():
        binary.chmod(0o700)
    log = tmp_path / "calls.log"
    state = tmp_path / "state"
    environment = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}",
                       MOCK_LOG=str(log), MOCK_STATE=str(state))
    if config_fail:
        environment["MOCK_CONFIG_FAIL"] = "1"
    if backup_fail:
        environment["MOCK_BACKUP_FAIL"] = "1"
    if unhealthy:
        environment["MOCK_UNHEALTHY_REVISION"] = REVISION
    if rollback_fail:
        environment["MOCK_ALL_UNHEALTHY"] = "1"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(root), REVISION, str(archive),
         hashlib.sha256(archive.read_bytes()).hexdigest()],
        env=environment, text=True, input="12345:" + "t" * 35,
        capture_output=True, timeout=45,
    )
    calls = log.read_text() if log.exists() else ""
    return result, root, old_release, calls


def test_success_advances_current_only_after_backup_and_health(tmp_path):
    result, root, _, calls = run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (root / "current/REVISION").read_text().strip() == REVISION
    assert calls.index("load_settings()") < calls.index("scripts/backup_database.py") < calls.index(" up ")
    assert "Deployment healthy" in result.stdout


def test_backup_failure_keeps_previous_release_running(tmp_path):
    result, root, previous, calls = run_deploy(tmp_path, previous=True, backup_fail=True)
    assert result.returncode == 7
    assert (root / "current").resolve() == previous
    assert calls.index("load_settings()") < calls.index("scripts/backup_database.py")
    assert " up " not in calls
    assert " stop " not in calls


def test_config_failure_skips_backup_and_keeps_previous_release_running(tmp_path):
    result, root, previous, calls = run_deploy(tmp_path, previous=True, config_fail=True)
    assert result.returncode == 8
    assert (root / "current").resolve() == previous
    assert "load_settings()" in calls
    assert "scripts/backup_database.py" not in calls
    assert " up " not in calls
    assert " stop " not in calls


def test_health_failure_restores_and_verifies_previous_release(tmp_path):
    result, root, previous, calls = run_deploy(tmp_path, previous=True, unhealthy=True)
    assert result.returncode != 0
    assert (root / "current").resolve() == previous
    assert "Previous application release is healthy" in result.stderr
    assert calls.count(" up ") == 2


def test_failed_rollback_does_not_claim_previous_release_is_healthy(tmp_path):
    result, _, _, _ = run_deploy(tmp_path, previous=True, unhealthy=True, rollback_fail=True)
    assert result.returncode != 0
    assert "ROLLBACK FAILED" in result.stderr
    assert "Previous application release is healthy" not in result.stderr


def test_first_failed_deployment_stops_replacement(tmp_path):
    result, root, _, calls = run_deploy(tmp_path, unhealthy=True)
    assert result.returncode != 0
    assert not (root / "current").exists()
    assert " stop " in calls
    assert "First deployment failed" in result.stderr


def test_archive_links_rejected_before_docker(tmp_path):
    result, _, _, calls = run_deploy(tmp_path, symlink=True)
    assert result.returncode != 0
    assert "may not contain symbolic or hard links" in result.stderr
    assert calls == ""
