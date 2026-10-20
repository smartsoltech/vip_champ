"""Inspect tracked files and optionally all reachable Git history without leaking values."""

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(rb"\b[0-9]{5,}:[A-Za-z0-9_-]{20,}\b")
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL)


def forbidden_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    return (
        any(part in {".history", ".cosine", ".venv", "__pycache__"} for part in parts)
        or (name.startswith(".env") and name != ".env.example")
        or name.endswith((".db", ".sqlite", ".sqlite3", ".dump", ".pem", ".key"))
    )


def inspect(path: str, content: bytes) -> list[str]:
    problems = []
    if forbidden_path(path):
        problems.append("forbidden tracked path")
    if TOKEN.search(content):
        problems.append("Telegram token pattern")
    if PRIVATE_KEY.search(content):
        problems.append("private key pattern")
    if Path(path).name == ".env.example":
        for raw in content.decode("utf-8", errors="replace").splitlines():
            key, separator, value = raw.partition("=")
            if not separator:
                continue
            key, value = key.strip(), value.strip().strip("\"'")
            if key == "DATABASE_URL":
                match = re.search(r"://[^:\s/]+:([^@\s]+)@", value)
                value = match.group(1) if match else ""
            elif key not in {"TELEGRAM_API_TOKEN", "MASTERADMIN_PASSWORD"}:
                continue
            if value and not re.search(r"(?i)(change_me|placeholder|your_|example)", value):
                problems.append("non-placeholder credential in env template")
                break
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    findings = []
    for name in git("ls-files", "-z").decode().split("\0"):
        if not name:
            continue
        path = ROOT / name
        if path.is_file():
            findings.extend((name, reason) for reason in inspect(name, path.read_bytes()))
    if args.history:
        for item in git("rev-list", "--objects", "--all").decode().splitlines():
            fields = item.split(" ", 1)
            if len(fields) != 2:
                continue
            oid, name = fields
            if git("cat-file", "-t", oid).strip() != b"blob":
                continue
            content = git("cat-file", "blob", oid)
            findings.extend(
                (f"history:{oid[:12]}:{name}", reason) for reason in inspect(name, content)
            )
    for name, reason in findings[:30]:
        print(f"{name}: {reason}")
    if findings:
        print(f"Secret/path checks failed: {len(findings)} findings; values are not printed.")
        return 1
    print("Tracked files and requested history checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
