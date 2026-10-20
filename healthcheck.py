"""Health requires a recent successful Telegram poll and database query."""

import json
import os
import time
from pathlib import Path


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) terminates processes on Windows rather than probing.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    os.kill(pid, 0)
    return True


def write_heartbeat(path: Path, revision: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"timestamp": time.time(), "pid": os.getpid(), "revision": revision}),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def is_healthy(path: Path, max_age: int, revision: str | None = None) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(data["timestamp"])
        if not 0 <= age <= max_age:
            return False
        if revision is not None and data.get("revision") != revision:
            return False
        pid = int(data["pid"])
        if pid <= 0:
            return False
        return _process_alive(pid)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main() -> int:
    from config import load_settings

    settings = load_settings(require_token=False)
    return 0 if is_healthy(
        settings.heartbeat_path, settings.health_max_age_seconds, settings.revision
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
