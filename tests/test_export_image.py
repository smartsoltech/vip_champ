import gzip
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import export_image

REVISION = "a" * 40
DIGEST = "sha256:" + "b" * 64
REGISTRY_CONFIG = json.dumps({"auths": {export_image.REGISTRY: {"auth": "fake-encoded-private-credential"}}})


def fake_registry(monkeypatch, *, revision=REVISION, architecture="amd64", failed=False):
    calls = []
    auth_paths = []
    config = json.dumps({
        "os": "linux", "architecture": architecture,
        "config": {"Labels": {"org.opencontainers.image.revision": revision}},
    }).encode()

    def run(arguments, **kwargs):
        calls.append(arguments)
        assert "REGISTRY_CONFIG" not in kwargs["env"]
        assert kwargs["timeout"] == 600
        auth_file = Path(arguments[arguments.index("--authfile") + 1])
        auth_paths.append(auth_file)
        if os.name == "posix":
            assert auth_file.stat().st_mode & 0o777 == 0o600
        auth = json.loads(auth_file.read_text())
        assert export_image.REGISTRY in auth["auths"]
        if failed:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"sensitive registry failure")
        if arguments[1] == "inspect":
            return SimpleNamespace(returncode=0, stdout=(DIGEST + "\n").encode())
        archive = Path(arguments[-1].removeprefix("docker-archive:"))
        with tarfile.open(archive, "w") as saved:
            content = json.dumps([{"Config": "config.json", "Layers": []}]).encode()
            for name, data in [("manifest.json", content), ("config.json", config)]:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                saved.addfile(member, io.BytesIO(data))
        return SimpleNamespace(returncode=0, stdout=b"copied")

    monkeypatch.setattr(export_image.subprocess, "run", run)
    return calls, auth_paths, config


def test_export_pins_registry_digest_and_records_exact_docker_image_id(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTRY_CONFIG", REGISTRY_CONFIG)
    calls, auth_paths, config = fake_registry(monkeypatch)

    image_id = export_image.export_image(tmp_path, REVISION, REGISTRY_CONFIG)

    assert image_id == "sha256:" + hashlib.sha256(config).hexdigest()
    assert calls[1][-2] == f"docker://{export_image.REPOSITORY}@{DIGEST}"
    assert "fake-encoded-private-credential" not in repr(calls)
    assert not any(path.exists() for path in auth_paths)
    assert (tmp_path / "image-id").read_text().strip() == image_id
    assert (tmp_path / "registry-digest").read_text().strip() == DIGEST
    assert (tmp_path / "revision").read_text().strip() == REVISION
    compressed = (tmp_path / "image.tar.gz").read_bytes()
    assert (tmp_path / "image-sha256").read_text().strip() == hashlib.sha256(compressed).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(compressed))) as saved:
        assert saved.extractfile("config.json").read() == config
    assert not list(tmp_path.glob(".image-export-*"))


@pytest.mark.parametrize("config", [{"revision": "f" * 40}, {"architecture": "arm64"}])
def test_wrong_revision_or_platform_is_not_exported(tmp_path, monkeypatch, config):
    _, auth_paths, _ = fake_registry(monkeypatch, **config)

    with pytest.raises(ValueError):
        export_image.export_image(tmp_path, REVISION, REGISTRY_CONFIG)

    assert list(tmp_path.iterdir()) == []
    assert not any(path.exists() for path in auth_paths)


def test_registry_error_is_sanitized_and_authfile_is_removed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRONE_COMMIT_SHA", REVISION)
    monkeypatch.setenv("REGISTRY_CONFIG", REGISTRY_CONFIG)
    _, auth_paths, _ = fake_registry(monkeypatch, failed=True)

    assert export_image.main() == 1

    captured = capsys.readouterr()
    assert "fake-encoded-private-credential" not in captured.out + captured.err
    assert "sensitive registry failure" not in captured.out + captured.err
    assert "Image export failed" in captured.err
    assert not any(path.exists() for path in auth_paths)
