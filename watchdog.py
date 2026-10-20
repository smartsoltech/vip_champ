"""Bound stalled polling, database I/O, worker failure and shutdown independently."""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


class LivenessWatchdog:
    def __init__(self, timeout, shutdown_timeout, worker, *, startup_timeout=600, clock=time.monotonic, terminate=os._exit):
        self.timeout = timeout
        self.shutdown_timeout = shutdown_timeout
        self.startup_timeout = startup_timeout
        self.worker = worker
        self.clock = clock
        self.terminate = terminate
        self.startup_started = clock()
        self.last_poll = self.startup_started
        self.started = False
        self.shutdown_started = None
        # SIGTERM can interrupt a lifecycle update on the main thread.
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="liveness-watchdog", daemon=True)

    def poll_succeeded(self):
        with self._state_lock:
            self.last_poll = self.clock()

    def mark_started(self):
        """Begin the ordinary liveness budget after migration and worker recovery."""
        with self._state_lock:
            if not self.started:
                self.last_poll = self.clock()
                self.started = True

    def begin_shutdown(self):
        with self._state_lock:
            if self.shutdown_started is None:
                self.shutdown_started = self.clock()

    def failure_reason(self):
        with self._state_lock:
            now = self.clock()
            if self.shutdown_started is not None:
                return "shutdown_timeout" if now - self.shutdown_started > self.shutdown_timeout else None
            if not self.started:
                return "startup_timeout" if now - self.startup_started > self.startup_timeout else None
            if now - self.last_poll > self.timeout:
                return "poll_or_database_stalled"
        application = self.worker()
        if application is not None and now - application.last_worker_progress > self.timeout:
            return "broadcast_worker_stalled"
        return None

    def check(self):
        reason = self.failure_reason()
        if reason:
            try:
                logger.critical("Watchdog terminating stalled process: %s", reason)
            finally:
                self.terminate(70)
        return reason

    def _run(self):
        while not self._stop.wait(1):
            self.check()

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
