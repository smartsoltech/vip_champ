"""Export a registry digest to a checked Docker archive for offline deployment."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REGISTRY = "git.smartsoltech.kr"
REPOSITORY = f"{REGISTRY}/trevor/vip-champ"


def export_image(artifact_dir: Path, revision: str, registry_config: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None or not registry_config:
        raise ValueError("Missing image export configuration")
    auth = json.loads(registry_config).get("auths", {}).get(REGISTRY, {}).get("auth")
    if not isinstance(auth, str) or not auth:
        raise ValueError("Missing registry authentication entry")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    child_env = {key: value for key, value in os.environ.items()
                 if key != "REGISTRY_CONFIG"}
    with tempfile.TemporaryDirectory(prefix="vip-image-auth-") as auth_dir:
        auth_file = Path(auth_dir) / "auth.json"
        descriptor = os.open(auth_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"auths": {REGISTRY: {"auth": auth}}}, stream)

        def skopeo(*arguments: str) -> bytes:
            result = subprocess.run(
                ["skopeo", *arguments], capture_output=True, check=False, timeout=600, env=child_env,
            )
            if result.returncode:
                raise RuntimeError("Registry inspection or copy failed")
            return result.stdout

        digest = skopeo(
            "inspect", "--authfile", str(auth_file), "--format", "{{.Digest}}",
            f"docker://{REPOSITORY}:{revision}",
        ).decode("ascii").strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ValueError("Invalid registry digest")
        with tempfile.TemporaryDirectory(prefix=".image-export-", dir=artifact_dir) as export_dir:
            archive = Path(export_dir) / "image.tar"
            skopeo(
                "copy", "--authfile", str(auth_file),
                f"docker://{REPOSITORY}@{digest}", f"docker-archive:{archive}",
            )
            with tarfile.open(archive, "r") as saved:
                manifest_file = saved.extractfile("manifest.json")
                if manifest_file is None:
                    raise ValueError("Missing Docker archive manifest")
                manifest = json.load(manifest_file)
                if not isinstance(manifest, list) or len(manifest) != 1:
                    raise ValueError("Expected exactly one image in Docker archive")
                member = saved.getmember(manifest[0]["Config"])
                if not member.isfile():
                    raise ValueError("Invalid image config entry")
                config_file = saved.extractfile(member)
                if config_file is None:
                    raise ValueError("Missing image config")
                config_bytes = config_file.read()
            config = json.loads(config_bytes)
            if config.get("config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != revision:
                raise ValueError("Exported image revision does not match the verified commit")
            if config.get("os") != "linux" or config.get("architecture") != "amd64":
                raise ValueError("Exported image must target linux/amd64")
            image_id = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
            compressed = Path(export_dir) / "image.tar.gz"
            with archive.open("rb") as source, compressed.open("wb") as target:
                with gzip.GzipFile(fileobj=target, mode="wb", filename="", mtime=0) as encoded_archive:
                    shutil.copyfileobj(source, encoded_archive)
            with compressed.open("rb") as stream:
                archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            os.replace(compressed, artifact_dir / "image.tar.gz")
            for name, value in {
                "image-id": image_id, "image-sha256": archive_hash,
                "revision": revision, "registry-digest": digest,
            }.items():
                (artifact_dir / name).write_text(value + "\n", encoding="ascii")
    return image_id


def main() -> int:
    try:
        image_id = export_image(
            Path(".artifacts"), os.environ.get("DRONE_COMMIT_SHA", ""),
            os.environ.get("REGISTRY_CONFIG", ""),
        )
    except Exception:
        # Neither registry errors nor credential-bearing command environments are logged.
        print("Image export failed; check registry access, image revision, and artifact storage.", file=sys.stderr)
        return 1
    print(f"Verified CI image exported: {image_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
