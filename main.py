"""Application lifecycle. No network or database access occurs on import."""

import logging
import re
import signal
from threading import Event

import telebot

from config import ConfigurationError, Settings, load_settings
from healthcheck import write_heartbeat
from watchdog import LivenessWatchdog


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...] = ()):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_TOKEN]", text)
        return re.sub(r"(postgres(?:ql)?(?:\+\w+)?://[^:\s/]+:)[^@\s]+@", r"\1[REDACTED]@", text)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter((
        settings.telegram_api_token, settings.masteradmin_password, settings.database_url,
    )))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    telebot.logger.handlers.clear()
    telebot.logger.propagate = True


def run(settings: Settings) -> None:
    from bot import setup_bot_handlers
    from db import Repository

    logger = logging.getLogger(__name__)
    stopped = Event()
    settings.heartbeat_path.unlink(missing_ok=True)
    repository = Repository(settings.database_url, backup_dir=settings.backup_dir)
    bot = None
    application = None
    previous_handlers = {}
    watchdog = LivenessWatchdog(
        settings.watchdog_timeout_seconds, settings.shutdown_timeout_seconds,
        lambda: application, startup_timeout=settings.startup_timeout_seconds,
    )

    def stop(signum, frame):
        watchdog.begin_shutdown()
        stopped.set()
        if bot is not None:
            bot.stop_polling()

    try:
        watchdog.start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, stop)
        repository.acquire_runtime_lock()
        repository.initialize()
        if stopped.is_set():
            return
        if settings.masteradmin_login:
            repository.ensure_bootstrap_admin(
                settings.masteradmin_login,
                settings.masteradmin_password,
                next(iter(settings.masteradmin_telegram_ids)),
            )
        if stopped.is_set():
            return
        bot = telebot.TeleBot(settings.telegram_api_token, threaded=True, num_threads=4)
        original_get_updates = bot.get_updates

        def healthy_get_updates(*args, **kwargs):
            updates = original_get_updates(*args, **kwargs)
            if stopped.is_set():
                return []
            try:
                if not repository.ping():
                    raise RuntimeError("Database readiness query failed")
            except Exception:
                # A lost ownership connection must terminate the worker too.
                # Container restart obtains a fresh singleton lock before recovery.
                stopped.set()
                bot.stop_polling()
                raise
            watchdog.poll_succeeded()
            if not stopped.is_set() and application is not None and application.worker_healthy and watchdog.failure_reason() is None:
                write_heartbeat(settings.heartbeat_path, settings.revision)
            return updates

        bot.get_updates = healthy_get_updates
        application = setup_bot_handlers(bot, repository, settings)
        if stopped.is_set():
            return
        application.start()
        if stopped.is_set():
            return
        watchdog.mark_started()
        logger.info("Starting bot revision=%s", settings.revision)
        while not stopped.is_set():
            try:
                bot.polling(
                    non_stop=False, timeout=10, long_polling_timeout=20,
                    allowed_updates=["message", "callback_query"],
                )
            except Exception:
                if stopped.is_set():
                    raise
                logger.exception("Polling failed; retrying")
            if not stopped.is_set():
                stopped.wait(5)
    finally:
        watchdog.begin_shutdown()
        try:
            settings.heartbeat_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove readiness heartbeat during shutdown")
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                logger.warning("Could not restore a signal handler during shutdown")
        try:
            if bot is not None:
                bot.stop_bot()
        finally:
            try:
                if application is not None:
                    application.stop()
            finally:
                try:
                    repository.close()
                finally:
                    watchdog.stop()


def main() -> int:
    try:
        settings = load_settings()
    except ConfigurationError as error:
        logging.basicConfig(level=logging.ERROR)
        logging.getLogger(__name__).error("Configuration error: %s", error)
        return 2
    configure_logging(settings)
    try:
        run(settings)
    except Exception:
        logging.getLogger(__name__).exception("Application failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
