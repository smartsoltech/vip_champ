"""Exercise artifact transfer and secret handling without SSH or a Docker daemon."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="CI transport requires Linux shell tools")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci_deploy.sh"
REVISION = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64
TOKEN = "12345:" + "t" * 35
FAKE = """#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import sys
name = Path(sys.argv[0]).name
args = sys.argv[1:]
entry = {'tool': name, 'args': args}
if name == 'git':
    if args[0] == 'archive':
        Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('--output='))).write_bytes(b'source archive')
    else:
        print('#!/bin/sh')
elif name == 'ssh':
    if 'mktemp -d' in args[-1]:
        print('/tmp/vip-champ-deploy.ABC12345')
    elif args[-1].startswith('bash '):
        entry['stdin_hash'] = hashlib.sha256(sys.stdin.buffer.read()).hexdigest()
elif name == 'scp':
    source = Path(args[-2])
    assert source.is_file()
    entry['source'] = str(source)
with Path(os.environ['MOCK_CI_LOG']).open('a') as log:
    log.write(json.dumps(entry) + '\\n')
"""


def run_ci(tmp_path, *, invalid_hash=False, stale_revision=False):
    artifact_dir = tmp_path / ".artifacts"
    artifact_dir.mkdir()
    archive = b"checked image archive"
    (artifact_dir / "image.tar.gz").write_bytes(archive)
    (artifact_dir / "image-id").write_text(IMAGE_ID)
    (artifact_dir / "image-sha256").write_text("0" * 64 if invalid_hash else hashlib.sha256(archive).hexdigest())
    (artifact_dir / "revision").write_text("f" * 40 if stale_revision else REVISION)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ["ssh", "scp", "git"]:
        path = binaries / name
        path.write_text(FAKE)
        path.chmod(0o700)
    log = tmp_path / "calls.jsonl"
    environment = dict(
        os.environ, PATH=f"{binaries}:{os.environ['PATH']}", MOCK_CI_LOG=str(log),
        DRONE_COMMIT_SHA=REVISION, DEPLOY_HOST="example.invalid", DEPLOY_USER="testdeploy",
        DEPLOY_PATH="/opt/vip_champ", DEPLOY_SSH_KEY="private-key-test-value",
        DEPLOY_KNOWN_HOSTS="example.invalid test-known-host", TELEGRAM_API_TOKEN=TOKEN,
    )
    result = subprocess.run(["sh", str(SCRIPT)], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=30)
    raw = log.read_text() if log.exists() else ""
    assert TOKEN not in raw + result.stdout + result.stderr
    assert "private-key-test-value" not in raw + result.stdout + result.stderr
    return result, [json.loads(line) for line in raw.splitlines()]


def test_ci_transfers_checked_image_and_sends_token_only_over_stdin(tmp_path):
    result, calls = run_ci(tmp_path)
    assert result.returncode == 0, result.stderr
    uploads = [call for call in calls if call["tool"] == "scp"]
    assert len(uploads) == 3
    assert any(call["args"][-1].endswith("/image.tar.gz") for call in uploads)
    deployment = next(call for call in calls if "stdin_hash" in call)
    assert deployment["stdin_hash"] == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert IMAGE_ID in deployment["args"][-1]
    assert REVISION in deployment["args"][-1]
    assert "StrictHostKeyChecking=yes" in deployment["args"]
    for call in uploads:
        if not call["source"].endswith(".artifacts/image.tar.gz"):
            assert not Path(call["source"]).exists()


@pytest.mark.parametrize("failure", ["hash", "revision"])
def test_invalid_artifact_stops_before_any_remote_access(tmp_path, failure):
    result, calls = run_ci(tmp_path, invalid_hash=failure == "hash", stale_revision=failure == "revision")
    assert result.returncode != 0
    assert calls == []
