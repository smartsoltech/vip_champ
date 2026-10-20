"""Exercise the real lifecycle, worker and temporary DB without Telegram traffic."""

import json
import signal
from pathlib import Path
from threading import Event

import pytest

import main
from bot import setup_bot_handlers as real_setup
from config import load_settings
from db import Repository as RealRepository


class UnexpectedPollingLoop(BaseException):
    """Escape a faulty retry loop without waiting forever in an offline test."""


class FastEvent:
    def __init__(self):
        self.event = Event()
        self.waits = []

    def set(self):
        self.event.set()

    def is_set(self):
        return self.event.is_set()

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.event.wait(0)


class FakePollingBot:
    def __init__(self, harness):
        self.harness = harness
        self.polls = 0
        self.telegram_calls = 0
        self.stop_polling_calls = 0
        self.stop_bot_calls = 0
        self.poll_options = []

    def message_handler(self, **kwargs):
        return lambda function: function

    def callback_query_handler(self, **kwargs):
        return lambda function: function

    def get_updates(self, *args, **kwargs):
        self.telegram_calls += 1
        self.harness.telegram_update_hook()
        if self.harness.telegram_error:
            raise RuntimeError("synthetic Telegram transport failure")
        return []

    def polling(self, **kwargs):
        self.polls += 1
        self.poll_options.append(kwargs)
        if self.polls > 8:
            raise UnexpectedPollingLoop("Lifecycle did not stop as requested")
        self.harness.poll_hook(self)

    def stop_polling(self):
        self.stop_polling_calls += 1

    def stop_bot(self):
        self.stop_bot_calls += 1
        if self.harness.stop_bot_error:
            raise RuntimeError("synthetic transport shutdown failure")


class LifecycleHarness:
    def __init__(self, tmp_path, monkeypatch):
        self.settings = load_settings(environ={
            "TELEGRAM_API_TOKEN": "123456:" + "x" * 35,
            "DATABASE_URL": f"sqlite:///{(tmp_path / 'runtime.db').as_posix()}",
            "BACKUP_DIR": str(tmp_path / "backups"),
            "HEARTBEAT_PATH": str(tmp_path / "heartbeat.json"),
            "APP_REVISION": "offline-lifecycle-test",
        })
        self.event = FastEvent()
        self.repository = None
        self.application = None
        self.worker_started = False
        self.worker_stopped = False
        self.telegram_error = False
        self.telegram_update_hook = lambda: None
        self.database_failure = None
        self.initialize_error = False
        self.start_worker_error = False
        self.stop_bot_error = False
        self.initialize_hook = lambda: None
        self.start_worker_hook = lambda: None
        self.watchdog_now = 100.0
        self.watchdog = None
        self.watchdog_exits = []
        self.poll_hook = lambda bot: self.request_stop()
        self.signals = {signal.SIGINT: object(), signal.SIGTERM: object()}
        self.original_signals = self.signals.copy()
        self.signal_calls = []
        self.transport = FakePollingBot(self)
        harness = self

        class TrackingRepository(RealRepository):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.close_calls = 0
                self.ping_calls = 0
                harness.repository = self

            def initialize(self):
                super().initialize()
                harness.initialize_hook()
                if harness.initialize_error:
                    raise RuntimeError("synthetic migration failure")

            def ping(self):
                self.ping_calls += 1
                if harness.database_failure == "raise":
                    raise RuntimeError("synthetic database query failure")
                if harness.database_failure == "false":
                    return False
                return super().ping()

            def close(self):
                self.close_calls += 1
                super().close()

        def setup(bot, repository, settings):
            app = real_setup(bot, repository, settings)
            self.application = app
            original_start = app.start
            original_stop = app.stop

            def start():
                original_start()
                self.worker_started = app._worker.is_alive()
                self.start_worker_hook()
                if self.start_worker_error:
                    raise RuntimeError("synthetic failure after starting worker")

            def stop():
                original_stop()
                self.worker_stopped = app._worker is not None and not app._worker.is_alive()

            app.start = start
            app.stop = stop
            return app

        def set_signal(signum, handler):
            previous = self.signals[signum]
            self.signal_calls.append((signum, handler))
            self.signals[signum] = handler
            return previous

        watchdog_type = main.LivenessWatchdog

        def make_watchdog(*args, **kwargs):
            self.watchdog = watchdog_type(*args, **kwargs, clock=lambda: self.watchdog_now, terminate=self.watchdog_exits.append)
            return self.watchdog

        monkeypatch.setattr("db.Repository", TrackingRepository)
        monkeypatch.setattr("bot.setup_bot_handlers", setup)
        monkeypatch.setattr(main.telebot, "TeleBot", lambda *args, **kwargs: self.transport)
        monkeypatch.setattr(main, "Event", lambda: self.event)
        monkeypatch.setattr(main.signal, "signal", set_signal)
        monkeypatch.setattr(main, "LivenessWatchdog", make_watchdog)

    def request_stop(self):
        self.signals[signal.SIGTERM](signal.SIGTERM, None)

    def run(self):
        main.run(self.settings)

    def assert_clean_shutdown(self, *, worker_expected=True):
        assert not self.settings.heartbeat_path.exists()
        assert self.repository.close_calls == 1
        assert self.signals == self.original_signals
        assert self.watchdog is not None
        assert not self.watchdog._thread.is_alive()
        if worker_expected:
            assert self.worker_started
            assert self.worker_stopped
            assert self.transport.stop_bot_calls == 1
            assert not self.application._worker.is_alive()


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    return LifecycleHarness(tmp_path, monkeypatch)


def test_successful_poll_and_database_query_refresh_heartbeat_then_shutdown_removes_it(lifecycle):
    observed = {}
    lifecycle.settings.heartbeat_path.write_text("stale heartbeat from previous process", encoding="utf-8")

    def poll(bot):
        observed["stale_removed"] = not lifecycle.settings.heartbeat_path.exists()
        observed["updates"] = bot.get_updates(timeout=10)
        observed["heartbeat"] = json.loads(lifecycle.settings.heartbeat_path.read_text(encoding="utf-8"))
        observed["ping_calls"] = lifecycle.repository.ping_calls
        lifecycle.request_stop()

    lifecycle.poll_hook = poll
    lifecycle.run()
    assert observed["stale_removed"]
    assert observed["updates"] == []
    assert observed["heartbeat"]["revision"] == "offline-lifecycle-test"
    assert observed["heartbeat"]["pid"] > 0
    assert observed["heartbeat"]["timestamp"] > 0
    assert observed["ping_calls"] == 1
    assert lifecycle.transport.stop_polling_calls == 1
    assert lifecycle.transport.poll_options[0]["allowed_updates"] == ["message", "callback_query"]
    lifecycle.assert_clean_shutdown()


def test_polling_exception_retries_and_a_later_success_can_stop_cleanly(lifecycle, caplog):
    def poll(bot):
        if bot.polls == 1:
            raise RuntimeError("synthetic retryable polling failure")
        bot.get_updates()
        lifecycle.request_stop()

    lifecycle.poll_hook = poll
    lifecycle.run()
    assert lifecycle.transport.polls == 2
    assert lifecycle.event.waits == [5]
    assert lifecycle.transport.telegram_calls == 1
    assert "Polling failed; retrying" in caplog.text
    lifecycle.assert_clean_shutdown()


@pytest.mark.parametrize("failure", ["telegram", "database_false", "database_raise"])
def test_failed_telegram_or_database_probe_does_not_refresh_heartbeat(lifecycle, failure):
    observed = {}

    def poll(bot):
        bot.get_updates()
        observed["before"] = lifecycle.settings.heartbeat_path.read_bytes()
        observed["before_ping_count"] = lifecycle.repository.ping_calls
        lifecycle.telegram_error = failure == "telegram"
        if failure.startswith("database_"):
            lifecycle.database_failure = failure.removeprefix("database_")
        try:
            bot.get_updates()
        except RuntimeError:
            observed["failure_raised"] = True
        observed["after"] = lifecycle.settings.heartbeat_path.read_bytes()
        observed["after_ping_count"] = lifecycle.repository.ping_calls
        lifecycle.request_stop()

    lifecycle.poll_hook = poll
    lifecycle.run()
    assert observed.get("failure_raised")
    assert observed["after"] == observed["before"]
    expected_pings = observed["before_ping_count"] + (failure != "telegram")
    assert observed["after_ping_count"] == expected_pings
    lifecycle.assert_clean_shutdown()


def test_initialization_failure_closes_repository_without_starting_transport(lifecycle):
    lifecycle.initialize_error = True
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        lifecycle.run()
    assert lifecycle.application is None
    assert lifecycle.transport.polls == 0
    lifecycle.assert_clean_shutdown(worker_expected=False)

@pytest.mark.parametrize("failure", ["false", "raise"])
def test_database_readiness_failure_stops_worker_instead_of_retrying_without_ownership(lifecycle, failure):
    lifecycle.database_failure = failure
    lifecycle.poll_hook = lambda bot: bot.get_updates()
    with pytest.raises(RuntimeError, match="database|Database"):
        lifecycle.run()
    assert lifecycle.transport.polls == 1
    assert lifecycle.event.waits == []
    assert not lifecycle.settings.heartbeat_path.exists()
    lifecycle.assert_clean_shutdown()


def test_failure_after_worker_start_still_stops_worker_and_restores_signals(lifecycle):
    lifecycle.start_worker_error = True
    with pytest.raises(RuntimeError, match="synthetic failure after starting worker"):
        lifecycle.run()
    assert lifecycle.transport.polls == 0
    lifecycle.assert_clean_shutdown()


def test_transport_shutdown_failure_does_not_skip_worker_or_repository_cleanup(lifecycle):
    lifecycle.stop_bot_error = True
    with pytest.raises(RuntimeError, match="synthetic transport shutdown failure"):
        lifecycle.run()
    lifecycle.assert_clean_shutdown()


def test_heartbeat_cleanup_error_does_not_skip_other_cleanup(lifecycle, monkeypatch, caplog):
    original_unlink = Path.unlink
    calls = 0

    def fail_final_unlink(path, *args, **kwargs):
        nonlocal calls
        if path == lifecycle.settings.heartbeat_path:
            calls += 1
            if calls > 1:
                raise PermissionError("synthetic heartbeat filesystem failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_final_unlink)
    lifecycle.run()
    assert lifecycle.repository.close_calls == 1
    assert lifecycle.worker_stopped
    assert lifecycle.signals == lifecycle.original_signals
    assert lifecycle.transport.stop_bot_calls == 1
    assert "heartbeat" in caplog.text.lower()


def test_slow_backup_and_recovery_use_startup_budget_then_reset_poll_deadline(lifecycle):
    observed = []

    def initialize():
        lifecycle.watchdog_now += 361
        assert not lifecycle.watchdog.started
        assert lifecycle.watchdog.failure_reason() is None
        assert not lifecycle.settings.heartbeat_path.exists()
        observed.append("migration")

    def worker_started():
        assert not lifecycle.watchdog.started
        assert lifecycle.watchdog.failure_reason() is None
        observed.append("recovery")

    def poll(bot):
        assert lifecycle.watchdog.started
        assert lifecycle.watchdog.last_poll == lifecycle.watchdog_now
        bot.get_updates()
        assert lifecycle.settings.heartbeat_path.exists()
        observed.append("poll")
        lifecycle.request_stop()

    lifecycle.initialize_hook = initialize
    lifecycle.start_worker_hook = worker_started
    lifecycle.poll_hook = poll
    lifecycle.run()
    assert observed == ["migration", "recovery", "poll"]
    assert lifecycle.watchdog_exits == []
    lifecycle.assert_clean_shutdown()


def test_signal_during_startup_gets_shutdown_budget_and_does_not_start_bot(lifecycle):
    def initialize():
        assert not lifecycle.watchdog.started
        lifecycle.request_stop()
        assert lifecycle.watchdog.shutdown_started == lifecycle.watchdog_now
        lifecycle.watchdog_now += 29
        assert lifecycle.watchdog.failure_reason() is None
        lifecycle.watchdog_now += 2
        assert lifecycle.watchdog.check() == "shutdown_timeout"

    lifecycle.initialize_hook = initialize
    lifecycle.run()
    assert lifecycle.watchdog_exits and set(lifecycle.watchdog_exits) == {70}
    assert lifecycle.application is None
    assert lifecycle.transport.polls == 0
    assert lifecycle.transport.stop_bot_calls == 0
    lifecycle.assert_clean_shutdown(worker_expected=False)


def test_signal_during_worker_start_does_not_enter_polling(lifecycle):
    lifecycle.start_worker_hook = lifecycle.request_stop
    lifecycle.run()
    assert lifecycle.transport.polls == 0
    assert not lifecycle.watchdog.started
    assert lifecycle.watchdog.shutdown_started is not None
    lifecycle.assert_clean_shutdown()


def test_stalled_worker_cannot_refresh_heartbeat_even_when_poll_and_database_succeed(lifecycle):
    def poll(bot):
        bot.get_updates()
        before = lifecycle.settings.heartbeat_path.read_bytes()
        application = lifecycle.application
        # Block the real worker independently from polling/readiness checks.
        with application._broadcast_lock:
            application.last_worker_progress = lifecycle.watchdog_now
            lifecycle.watchdog_now += lifecycle.settings.watchdog_timeout_seconds + 1
            bot.get_updates()
            assert lifecycle.settings.heartbeat_path.read_bytes() == before
            assert lifecycle.watchdog.check() == "broadcast_worker_stalled"
        lifecycle.request_stop()

    lifecycle.poll_hook = poll
    lifecycle.run()
    assert lifecycle.watchdog_exits and set(lifecycle.watchdog_exits) == {70}
    lifecycle.assert_clean_shutdown()


def test_unhealthy_worker_does_not_refresh_readiness_heartbeat(lifecycle):
    def poll(bot):
        bot.get_updates()
        before = lifecycle.settings.heartbeat_path.read_bytes()
        with lifecycle.application._broadcast_lock:
            lifecycle.application.worker_healthy = False
            bot.get_updates()
            assert lifecycle.settings.heartbeat_path.read_bytes() == before
        lifecycle.request_stop()

    lifecycle.poll_hook = poll
    lifecycle.run()
    lifecycle.assert_clean_shutdown()


def test_late_poll_response_after_signal_does_not_probe_db_or_refresh_heartbeat(lifecycle):
    def poll(bot):
        bot.get_updates()
        before = lifecycle.settings.heartbeat_path.read_bytes()
        ping_count = lifecycle.repository.ping_calls
        lifecycle.telegram_update_hook = lifecycle.request_stop
        assert bot.get_updates() == []
        assert lifecycle.repository.ping_calls == ping_count
        assert lifecycle.settings.heartbeat_path.read_bytes() == before

    lifecycle.poll_hook = poll
    lifecycle.run()
    lifecycle.assert_clean_shutdown()
