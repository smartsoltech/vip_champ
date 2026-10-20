"""Validate candidate configuration and authenticate Telegram without polling."""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, load_settings


def verify_telegram(settings: Settings) -> None:
    with requests.get(
        f"https://api.telegram.org/bot{settings.telegram_api_token}/getMe",
        timeout=(5, 10),
        allow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise ValueError("Telegram rejected the candidate credentials")
        payload = response.json()
    account = payload.get("result", {})
    if (payload.get("ok") is not True or account.get("is_bot") is not True
            or not isinstance(account.get("id"), int) or account["id"] <= 0):
        raise ValueError("Telegram did not return a valid bot account")


def main() -> int:
    try:
        settings = load_settings()
        verify_telegram(settings)
    except Exception:
        # Requests exceptions can contain the complete credential-bearing URL.
        print("Candidate preflight failed; verify configuration, token, and Telegram connectivity.", file=sys.stderr)
        return 1
    # The deployer waits for startup, plus healthcheck scheduling/retry allowance.
    print(max(720, settings.startup_timeout_seconds + 120))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
