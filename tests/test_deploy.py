"""Exercise image/configuration promotion with fake Docker, never a real daemon."""

import hashlib
import io
import json
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
IMAGE_ID = "sha256:" + "c" * 64
PREVIOUS_IMAGE = "sha256:" + "d" * 64
NEW_TOKEN = "12345:" + "t" * 35
OLD_TOKEN = "54321:" + "s" * 35
ORIGINAL_ENV = f"# preserved settings\nTELEGRAM_API_TOKEN={OLD_TOKEN}\nDATABASE_URL=postgresql://example\nOTHER='keep spaces'\n"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"
TOKEN_WRITER = SCRIPT.with_name("set_bot_token.py")

DOCKER = """#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import sys
args = sys.argv[1:]
state = Path(os.environ['MOCK_STATE'])
env_file = Path(os.environ['ENV_FILE']) if os.environ.get('ENV_FILE') else None
env_hash = hashlib.sha256(env_file.read_bytes()).hexdigest() if env_file and env_file.exists() else None
with Path(os.environ['MOCK_LOG']).open('a') as log:
    log.write(json.dumps({'args': args, 'env_hash': env_hash,
                          'image': os.environ.get('APP_IMAGE'),
                          'revision': os.environ.get('APP_REVISION')}) + '\\n')
if args[0] == 'compose':
    if 'run' in args:
        # The production Compose 2.26 CLI has --pull for up, but not run.
        if '--pull' in args:
            raise SystemExit(125)
        if args[-1] == 'scripts/preflight.py':
            print(720)
            if os.environ.get('MOCK_OPERATOR_EDIT'):
                Path(os.environ['MOCK_SHARED_ENV']).write_text('OTHER=operator-edit\\n')
            raise SystemExit(int(os.environ.get('MOCK_PREFLIGHT_FAIL', '0')))
        if args[-1] == 'scripts/backup_database.py':
            raise SystemExit(7 if os.environ.get('MOCK_BACKUP_FAIL') else 0)
        raise SystemExit(9)
    if 'up' in args:
        state.write_text(json.dumps({'revision': os.environ['APP_REVISION'],
                                     'image': os.environ['APP_IMAGE'], 'env_hash': env_hash}))
    if 'ps' in args:
        print('fake-container')
elif args[:2] == ['image', 'inspect']:
    if '.Id' in args[-1]:
        if args[2].startswith('vip-champ:'):
            print(os.environ['MOCK_PREVIOUS_IMAGE'])
        else:
            print('sha256:' + '0' * 64 if os.environ.get('MOCK_IMAGE_MISMATCH') else args[2])
    else:
        print(os.environ['APP_REVISION'])
elif args[0] == 'inspect':
    current = json.loads(state.read_text())
    template = args[-1]
    if 'State.Status' in template:
        print('running')
    elif 'State.Health' in template:
        unhealthy = current['revision'] == os.environ.get('MOCK_UNHEALTHY_REVISION') or os.environ.get('MOCK_ALL_UNHEALTHY')
        print('unhealthy' if unhealthy else 'healthy')
    elif template == '{{.Image}}':
        print(current['image'])
    else:
        print(current['revision'])
"""

MOVE = """#!/usr/bin/env python3
import os
import subprocess
import sys
if os.environ.get('MOCK_LINK_FAIL') and sys.argv[-2].endswith('.next'):
    raise SystemExit(17)
if os.environ.get('MOCK_AFTER_LINK_FAIL') and sys.argv[-2].endswith('.next'):
    subprocess.run(['/bin/mv', *sys.argv[1:]], check=True)
    raise SystemExit(18)
os.execv('/bin/mv', ['mv', *sys.argv[1:]])
"""


def run_deploy(tmp_path, *, previous=False, preflight_fail=0, backup_fail=False, unhealthy=False,
               rollback_fail=False, symlink=False, token=NEW_TOKEN, image_mismatch=False,
               bad_image_hash=False, link_fail=False, operator_edit=False, legacy_previous=False,
               env_mode=0o600, after_link_fail=False):
    root = tmp_path / "application"
    (root / "shared/db/backups").mkdir(parents=True)
    env_file = root / "shared/.env"
    env_file.write_text(ORIGINAL_ENV)
    env_file.chmod(env_mode)
    old_release = root / "releases/previous"
    if previous:
        old_release.mkdir(parents=True)
        (old_release / "REVISION").write_text(PREVIOUS)
        if not legacy_previous:
            (old_release / "IMAGE_ID").write_text(PREVIOUS_IMAGE)
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
    image_archive = tmp_path / "image.tar.gz"
    image_archive.write_bytes(b"fake checked Docker artifact")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text(DOCKER)
    (binaries / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (binaries / "seq").write_text("#!/bin/sh\nprintf '1\\n2\\n'\n")
    (binaries / "mv").write_text(MOVE)
    for binary in binaries.iterdir():
        binary.chmod(0o700)
    log = tmp_path / "calls.log"
    state = tmp_path / "state"
    environment = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}",
                       MOCK_LOG=str(log), MOCK_STATE=str(state), MOCK_PREVIOUS_IMAGE=PREVIOUS_IMAGE,
                       MOCK_SHARED_ENV=str(env_file))
    environment["MOCK_PREFLIGHT_FAIL"] = str(preflight_fail)
    for enabled, key in [
        (backup_fail, "MOCK_BACKUP_FAIL"), (rollback_fail, "MOCK_ALL_UNHEALTHY"),
        (image_mismatch, "MOCK_IMAGE_MISMATCH"), (link_fail, "MOCK_LINK_FAIL"),
        (operator_edit, "MOCK_OPERATOR_EDIT"),
        (after_link_fail, "MOCK_AFTER_LINK_FAIL"),
    ]:
        if enabled:
            environment[key] = "1"
    if unhealthy:
        environment["MOCK_UNHEALTHY_REVISION"] = REVISION
    image_hash = "0" * 64 if bad_image_hash else hashlib.sha256(image_archive.read_bytes()).hexdigest()
    result = subprocess.run(
        ["bash", str(SCRIPT), str(root), REVISION, str(archive),
         hashlib.sha256(archive.read_bytes()).hexdigest(), str(image_archive), image_hash, IMAGE_ID],
        env=environment, text=True, input=token, capture_output=True, timeout=45,
    )
    logged = log.read_text() if log.exists() else ""
    assert NEW_TOKEN not in result.stdout + result.stderr + logged
    assert OLD_TOKEN not in result.stdout + result.stderr + logged
    assert not list((root / "shared").glob(".env.candidate.*"))
    assert not list((root / "shared").glob(".env.previous.*"))
    calls = [json.loads(line) for line in logged.splitlines()]
    return result, root, old_release, calls


def up_calls(calls):
    return [entry for entry in calls if "up" in entry["args"]]


def test_success_promotes_exact_ci_image_and_candidate_env(tmp_path):
    result, root, _, calls = run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (root / "current/REVISION").read_text().strip() == REVISION
    assert (root / "current/IMAGE_ID").read_text().strip() == IMAGE_ID
    assert (root / "shared/.env").read_text() == ORIGINAL_ENV.replace(OLD_TOKEN, NEW_TOKEN)
    args = [entry["args"] for entry in calls]
    assert not any("build" in call or "pull" == call[0] for call in args)
    assert next(i for i, call in enumerate(args) if call[0] == "load") < next(
        i for i, call in enumerate(args) if call[-1] == "scripts/preflight.py")
    assert next(i for i, call in enumerate(args) if call[-1] == "scripts/preflight.py") < next(
        i for i, call in enumerate(args) if call[-1] == "scripts/backup_database.py")
    assert up_calls(calls)[0]["image"] == IMAGE_ID
    assert "Deployment healthy" in result.stdout


@pytest.mark.parametrize("phase", ["syntax", "telegram", "backup"])
def test_pre_replacement_failure_keeps_original_env_and_service(tmp_path, phase):
    kwargs = {"backup_fail": True} if phase == "backup" else {"preflight_fail": 8 if phase == "syntax" else 12}
    result, root, previous, calls = run_deploy(tmp_path, previous=True, **kwargs)
    assert result.returncode in {7, 8, 12}
    assert (root / "current").resolve() == previous
    assert (root / "shared/.env").read_text() == ORIGINAL_ENV
    assert not up_calls(calls)
    assert not any("stop" in entry["args"] for entry in calls)
    if phase != "backup":
        assert not any(entry["args"][-1] == "scripts/backup_database.py" for entry in calls)


def test_malformed_token_cannot_modify_original_environment(tmp_path):
    result, root, previous, calls = run_deploy(tmp_path, previous=True, token="malformed-private-value")
    assert result.returncode != 0
    assert (root / "current").resolve() == previous
    assert (root / "shared/.env").read_text() == ORIGINAL_ENV
    assert calls == []
    assert "malformed-private-value" not in result.stderr


@pytest.mark.parametrize("legacy", [False, True])
def test_health_failure_restores_previous_image_with_original_token(tmp_path, legacy):
    result, root, previous, calls = run_deploy(tmp_path, previous=True, unhealthy=True, legacy_previous=legacy)
    assert result.returncode != 0
    assert (root / "current").resolve() == previous
    assert (root / "shared/.env").read_text() == ORIGINAL_ENV
    replacements = up_calls(calls)
    assert len(replacements) == 2
    assert replacements[0]["env_hash"] == hashlib.sha256(ORIGINAL_ENV.replace(OLD_TOKEN, NEW_TOKEN).encode()).hexdigest()
    assert replacements[1]["env_hash"] == hashlib.sha256(ORIGINAL_ENV.encode()).hexdigest()
    assert replacements[1]["image"] == PREVIOUS_IMAGE
    assert "Previous application release and environment are healthy" in result.stderr


def test_failure_after_env_promotion_restores_original_permissions_and_token(tmp_path):
    result, root, previous, calls = run_deploy(tmp_path, previous=True, link_fail=True, env_mode=0o400)
    assert result.returncode == 17
    assert (root / "current").resolve() == previous
    assert (root / "shared/.env").read_text() == ORIGINAL_ENV
    assert (root / "shared/.env").stat().st_mode & 0o777 == 0o400
    assert up_calls(calls)[-1]["env_hash"] == hashlib.sha256(ORIGINAL_ENV.encode()).hexdigest()


def test_failed_rollback_does_not_claim_success(tmp_path):
    result, root, _, _ = run_deploy(tmp_path, previous=True, unhealthy=True, rollback_fail=True)
    assert result.returncode != 0
    assert "ROLLBACK FAILED" in result.stderr
    assert "Previous application release and environment are healthy" not in result.stderr
    assert (root / "shared/.env").read_text() == ORIGINAL_ENV


@pytest.mark.parametrize("previous", [False, True])
def test_failure_after_current_promotion_restores_release_pointer_and_env(tmp_path, previous):
    result, root, old_release, calls = run_deploy(tmp_path, previous=previous, after_link_fail=True)
    assert result.returncode == 18
    assert (root / "shared/.env").read_text() == ORIGINAL_ENV
    if previous:
        assert (root / "current").resolve() == old_release
        assert up_calls(calls)[-1]["image"] == PREVIOUS_IMAGE
    else:
        assert not (root / "current").exists()
        assert "First deployment failed" in result.stderr


def test_first_failed_deployment_stops_replacement_and_preserves_env(tmp_path):
    result, root, _, calls = run_deploy(tmp_path, unhealthy=True)
    assert result.returncode != 0
    assert not (root / "current").exists()
    assert (root / "shared/.env").read_text() == ORIGINAL_ENV
    assert any("stop" in entry["args"] for entry in calls)
    assert "First deployment failed" in result.stderr


@pytest.mark.parametrize("kind", ["checksum", "identity"])
def test_wrong_image_is_rejected_before_preflight(tmp_path, kind):
    result, root, _, calls = run_deploy(
        tmp_path, bad_image_hash=kind == "checksum", image_mismatch=kind == "identity",
    )
    assert result.returncode != 0
    assert (root / "shared/.env").read_text() == ORIGINAL_ENV
    assert not any(entry["args"][0] == "compose" for entry in calls)


def test_operator_config_edit_is_preserved_without_replacing_service(tmp_path):
    result, root, _, calls = run_deploy(tmp_path, previous=True, operator_edit=True)
    assert result.returncode != 0
    assert (root / "shared/.env").read_text() == "OTHER=operator-edit\n"
    assert not up_calls(calls)


def test_archive_links_rejected_before_docker(tmp_path):
    result, _, _, calls = run_deploy(tmp_path, symlink=True)
    assert result.returncode != 0
    assert "may not contain symbolic or hard links" in result.stderr
    assert calls == []
