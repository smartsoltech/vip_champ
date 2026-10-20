import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from watchdog import LivenessWatchdog


def fixture():
    now = [100.0]
    worker = SimpleNamespace(last_worker_progress=100.0)
    exits = []
    guard = LivenessWatchdog(180, 30, lambda: worker, clock=lambda: now[0], terminate=exits.append)
    guard.mark_started()
    return now, worker, exits, guard


def test_successful_poll_does_not_hide_stalled_worker():
    now, _, exits, guard = fixture()
    now[0] += 181
    guard.poll_succeeded()
    assert guard.check() == "broadcast_worker_stalled"
    assert exits == [70]


def test_active_worker_does_not_hide_stalled_database_or_poll():
    now, worker, exits, guard = fixture()
    now[0] += 181
    worker.last_worker_progress = now[0]
    assert guard.check() == "poll_or_database_stalled"
    assert exits == [70]


def test_intentional_shutdown_has_separate_bounded_grace():
    now, _, exits, guard = fixture()
    guard.begin_shutdown()
    now[0] += 29
    guard.begin_shutdown()
    assert guard.check() is None
    now[0] += 2
    assert guard.check() == "shutdown_timeout"
    assert exits == [70]


def test_long_running_healthy_work_refreshes_both_deadlines():
    now, worker, exits, guard = fixture()
    for _ in range(10):
        now[0] += 60
        worker.last_worker_progress = now[0]
        guard.poll_succeeded()
        assert guard.check() is None
    assert exits == []


def test_startup_allows_backup_and_validation_beyond_normal_liveness_budget():
    now = [100.0]
    worker = SimpleNamespace(last_worker_progress=100.0)
    exits = []
    guard = LivenessWatchdog(180, 30, lambda: worker, startup_timeout=600, clock=lambda: now[0], terminate=exits.append)
    now[0] += 361
    assert guard.check() is None
    worker.last_worker_progress = now[0]
    guard.mark_started()
    assert guard.last_poll == now[0]
    now[0] += 179
    assert guard.check() is None
    # Marking ready twice must not extend a stalled poll's deadline.
    guard.mark_started()
    now[0] += 2
    assert guard.check() == "poll_or_database_stalled"
    assert exits == [70]


def test_stalled_startup_is_bounded_and_successful_polls_do_not_extend_it():
    now = [100.0]
    exits = []
    guard = LivenessWatchdog(180, 30, lambda: None, startup_timeout=600, clock=lambda: now[0], terminate=exits.append)
    now[0] += 601
    guard.poll_succeeded()
    assert guard.check() == "startup_timeout"
    assert exits == [70]


def test_shutdown_budget_overrides_startup_and_cannot_be_reset_by_readiness():
    now = [100.0]
    exits = []
    guard = LivenessWatchdog(180, 30, lambda: None, startup_timeout=600, clock=lambda: now[0], terminate=exits.append)
    now[0] += 100
    guard.begin_shutdown()
    now[0] += 29
    guard.mark_started()
    guard.begin_shutdown()
    assert guard.check() is None
    now[0] += 2
    assert guard.check() == "shutdown_timeout"
    assert exits == [70]


@pytest.mark.parametrize("phase,reason", [
    ("startup", "startup_timeout"),
    ("runtime", "poll_or_database_stalled"),
    ("shutdown", "shutdown_timeout"),
])
def test_watchdog_thread_really_exits_a_blocked_subprocess(phase, reason):
    code = "\n".join([
        "import sys, threading",
        "from watchdog import LivenessWatchdog",
        "guard = LivenessWatchdog(0.1, 0.1, lambda: None, startup_timeout=0.1)",
        "if sys.argv[1] == 'runtime': guard.mark_started()",
        "if sys.argv[1] == 'shutdown': guard.begin_shutdown()",
        "guard.start()",
        "threading.Event().wait(10)",
        "raise SystemExit(99)",
    ])
    started = time.monotonic()
    result = subprocess.run([sys.executable, '-c', code, phase], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
    assert result.returncode == 70
    assert reason in result.stderr
    assert 0.1 <= time.monotonic() - started < 5
