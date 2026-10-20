from unittest.mock import Mock

import pytest
import requests

from config import Settings
from scripts import preflight

TOKEN = "12345678:" + "x" * 35


def response(status=200, payload=None):
    result = Mock(status_code=status)
    result.json.return_value = payload or {"ok": True, "result": {"id": 12345678, "is_bot": True}}
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def test_candidate_check_authenticates_without_polling(monkeypatch):
    request = Mock(return_value=response())
    monkeypatch.setattr(preflight.requests, "get", request)

    preflight.verify_telegram(Settings(telegram_api_token=TOKEN))

    request.assert_called_once_with(
        f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=(5, 10), allow_redirects=False,
    )


@pytest.mark.parametrize("reply", [
    response(status=401), response(status=302),
    response(payload={"ok": False}),
    response(payload={"ok": True, "result": {"id": 123, "is_bot": False}}),
])
def test_rejected_candidate_is_never_accepted(monkeypatch, reply):
    monkeypatch.setattr(preflight.requests, "get", Mock(return_value=reply))
    with pytest.raises(ValueError):
        preflight.verify_telegram(Settings(telegram_api_token=TOKEN))


def test_network_error_does_not_expose_token(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "load_settings", lambda: Settings(telegram_api_token=TOKEN))
    monkeypatch.setattr(preflight.requests, "get", Mock(side_effect=requests.Timeout(f"URL contains {TOKEN}")))

    assert preflight.main() == 1

    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "Candidate preflight failed" in captured.err


def test_success_reports_configured_startup_budget_with_health_allowance(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "load_settings", lambda: Settings(
        telegram_api_token=TOKEN, startup_timeout_seconds=1200,
    ))
    monkeypatch.setattr(preflight.requests, "get", Mock(return_value=response()))

    assert preflight.main() == 0

    assert capsys.readouterr().out == "1320\n"
