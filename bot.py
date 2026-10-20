"""Private administrative conversations and durable broadcasts."""

import io
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

from telebot import apihelper

from kb import (
    generate_admin_inline_keyboard,
    generate_confirmation_keyboard,
    generate_contact_keyboard,
    normalize_telegram_url,
)

logger = logging.getLogger(__name__)
MAX_TEXT_LENGTH = 4096
MAX_IMPORT_BYTES = 5 * 1024 * 1024
_SUPERADMIN_ACTIONS = {"add_admin", "add_superadmin", "remove_admin", "get_admins"}
_SETTING_ACTIONS = {
    "change_manager_contact": "manager_contact",
    "change_channel_link": "channel_link",
    "change_bot_link": "bot_link",
}
_INPUT_ACTIONS = set(_SETTING_ACTIONS) | {
    "add_admin", "add_superadmin", "remove_admin", "send_all", "post_channel", "import_clients"
}
_ACTIONS = _INPUT_ACTIONS | {"get_admins", "export_clients", "get_settings", "check_admin"}
_CONTENT_TYPES = [
    "text", "document", "photo", "sticker", "voice", "audio", "video", "video_note",
    "animation", "contact", "location", "venue", "dice",
]


@dataclass
class Conversation:
    action: str
    expires: float
    username: str | None = None


@dataclass
class Menu:
    nonce: str
    message_id: int
    expires: float


@dataclass(frozen=True)
class AuthSession:
    expires: float
    auth_revision: str


@dataclass(frozen=True)
class Confirmation:
    nonce: str
    message_id: int
    expires: float
    action: str
    text: str
    auth_revision: str
    targets: tuple[int, ...] = ()
    configured_targets: tuple[int | str, ...] = ()
    contact_settings: dict | None = None


@dataclass(frozen=True)
class PendingDelivery:
    job_id: int
    actor_id: int
    chat_id: int
    status: str
    error: str | None
    retry_after: float | None
    attempt: int


class BotApplication:
    def __init__(self, bot, repository, settings, *, clock=time.monotonic):
        self.bot = bot
        self.repository = repository
        self.settings = settings
        self.clock = clock
        self._states = {}
        self._sessions = {}
        self._menus = {}
        self._confirmations = {}
        self._attempts = defaultdict(deque)
        self._global_attempts = deque()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._conversation_locks = {}
        self._broadcast_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker = None
        self._notified_jobs = deque(maxlen=1024)
        self._next_send_at = 0.0
        self._pending_delivery = None
        self.last_worker_progress = self.clock()
        self.worker_healthy = True
        self.bot.message_handler(content_types=_CONTENT_TYPES)(self.handle_message)
        self.bot.callback_query_handler(func=lambda call: True)(self.handle_callback)

    @staticmethod
    def _private(message, user=None):
        user = user or getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        return (
            user is not None
            and not getattr(user, "is_bot", False)
            and chat is not None
            and getattr(chat, "type", None) == "private"
            and chat.id == user.id
        )

    def _send(self, chat_id, text, **kwargs):
        return self.bot.send_message(chat_id, text, **kwargs)

    def _safe_notice(self, chat_id, text):
        try:
            self._send(chat_id, text)
        except Exception as error:  # noqa: BLE001 - contain transport errors without logging request data.
            logger.warning("Notification failed (%s)", type(error).__name__)

    def _key(self, message):
        return message.chat.id, message.from_user.id

    @contextmanager
    def _conversation(self, key):
        """Serialize one user's input without blocking another user's I/O."""
        with self._lock:
            entry = self._conversation_locks.get(key)
            if entry is None:
                entry = [threading.RLock(), 0]
                self._conversation_locks[key] = entry
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._lock:
                entry[1] -= 1
                if entry[1] == 0:
                    self._conversation_locks.pop(key, None)

    def _pop_state(self, key):
        with self._lock:
            self._states.pop(key, None)
            self._confirmations.pop(key, None)

    def _clear(self, key):
        with self._lock:
            self._states.pop(key, None)
            self._menus.pop(key, None)
            self._confirmations.pop(key, None)

    def _cleanup(self):
        with self._lock:
            now = self.clock()
            for collection in (self._states, self._menus, self._confirmations):
                for key, value in list(collection.items()):
                    if value.expires <= now:
                        collection.pop(key, None)
            for key, session in list(self._sessions.items()):
                if session.expires <= now:
                    self._sessions.pop(key, None)
                    self._clear(key)
            oldest = now - self.settings.auth_window_seconds
            for user_id, attempts in list(self._attempts.items()):
                while attempts and attempts[0] <= oldest:
                    attempts.popleft()
                if not attempts:
                    self._attempts.pop(user_id, None)
            while self._global_attempts and self._global_attempts[0] <= oldest:
                self._global_attempts.popleft()

    def _set_state(self, message, action, *, username=None):
        with self._lock:
            self._states[self._key(message)] = Conversation(
                action, self.clock() + self.settings.state_ttl_seconds, username
            )

    def _admin(self, message, *, superadmin=False):
        key = self._key(message)
        with self._lock:
            session = self._sessions.get(key)
        if not self._private(message) or session is None or session.expires <= self.clock():
            self._clear(key)
            self._safe_notice(message.chat.id, "Для управления войдите через /settings в личном чате.")
            return None
        admin = self.repository.get_admin_by_telegram_id(message.from_user.id)
        if admin is None:
            with self._lock:
                self._sessions.pop(key, None)
            self._clear(key)
            self._safe_notice(message.chat.id, "Доступ отозван. Обратитесь к администратору.")
            return None
        if not secrets.compare_digest(session.auth_revision, admin.auth_revision):
            with self._lock:
                self._sessions.pop(key, None)
            self._clear(key)
            self._safe_notice(message.chat.id, "Данные доступа изменились. Войдите снова через /settings.")
            return None
        if superadmin and not admin.is_superadmin:
            self._clear(key)
            self._safe_notice(message.chat.id, "Эта операция доступна только суперадминистратору.")
            return None
        return admin

    def _show_menu(self, message, admin=None):
        admin = admin or self._admin(message)
        if admin is None:
            return
        nonce = secrets.token_urlsafe(12)
        sent = self._send(
            message.chat.id, "Выберите действие:",
            reply_markup=generate_admin_inline_keyboard(nonce, admin.is_superadmin),
        )
        with self._lock:
            session = self._sessions.get(self._key(message))
            self._menus[self._key(message)] = Menu(
                nonce, sent.message_id,
                min(self.clock() + self.settings.state_ttl_seconds, session.expires if session else 0),
            )

    def handle_message(self, message):
        chat = getattr(message, "chat", None)
        if chat is None:
            return
        user = getattr(message, "from_user", None)
        with self._conversation((chat.id, getattr(user, "id", None))):
            try:
                self._cleanup()
                self._handle_message(message)
            except Exception as error:  # noqa: BLE001 - final handler boundary must not stop polling.
                logger.warning("Message operation failed (%s)", type(error).__name__)
                if getattr(message, "from_user", None):
                    self._pop_state(self._key(message))
                self._safe_notice(chat.id, "Не удалось выполнить операцию. Попробуйте позже.")

    def _handle_message(self, message):
        if getattr(message, "from_user", None) is None or message.from_user.is_bot:
            return
        text = getattr(message, "text", None)
        command = None
        if isinstance(text, str) and text.startswith("/"):
            command = text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
            if command == "admin":
                command = "settings"
        if not self._private(message):
            if command in _ACTIONS | {"settings", "get_settings", "broadcast_status", "broadcast_cancel", "cancel", "stop"}:
                self._safe_notice(message.chat.id, "Управление доступно только в личном чате с ботом.")
            return
        key = self._key(message)
        with self._lock:
            state = self._states.get(key)
        if state and state.action == "login_password" and command not in {"start", "help", "cancel", "stop", "settings"}:
            self._login(message, username=state.username)
            return
        if command == "stop":
            self._clear(key)
            self.repository.set_client_subscription(message.chat.id, False)
            self._send(message.chat.id, "Вы отписаны от рассылок. Уже начатая отправка может завершиться. /start — подписаться снова.")
            return
        if command in {"start", "help", "cancel"}:
            self._clear(key)
            if command == "cancel":
                self._send(message.chat.id, "Ввод и неподтверждённая отправка отменены. Для остановки рассылки в очереди: /broadcast_cancel НОМЕР.")
                with self._lock:
                    authenticated = key in self._sessions
                if authenticated:
                    self._show_menu(message)
            else:
                if command == "start":
                    self.repository.get_or_create_client(
                        chat_id=message.chat.id, first_name=message.from_user.first_name,
                        last_name=message.from_user.last_name, is_bot=False, subscribed=True,
                    )
                markup = generate_contact_keyboard(self.repository.get_settings())
                heading = "Выберите опцию:" if markup else "Контакты пока не настроены. Попробуйте позже."
                self._send(
                    message.chat.id,
                    heading + "\n/stop — отписаться от рассылок. /admin — управление.",
                    reply_markup=markup,
                )
            return
        if command == "settings":
            self._clear(key)
            with self._lock:
                self._sessions.pop(key, None)
            self._set_state(message, "login")
            self._send(message.chat.id, "Введите логин и пароль через пробел или только логин, чтобы ввести пароль отдельным сообщением. Отмена: /cancel.")
            return
        if command == "broadcast_status":
            self._pop_state(key)
            self._broadcast_status(message)
            return
        if command == "broadcast_cancel":
            self._pop_state(key)
            self._broadcast_cancel(message)
            return
        if command in _ACTIONS:
            self._pop_state(key)
            self._begin_action(message, command)
            return
        if command:
            self._send(message.chat.id, "Неизвестная команда. /start — меню, /admin — управление, /cancel — отмена.")
            return
        with self._lock:
            state = self._states.get(key)
            confirmation = self._confirmations.get(key)
        if state:
            self._process_state(message, state)
        elif confirmation:
            self._send(message.chat.id, "Проверьте предпросмотр и нажмите «Подтвердить отправку» или /cancel.")
        elif text is not None:
            self._send(message.chat.id, "Откройте /start для контактов или /settings для управления.")

    def handle_callback(self, call):
        notice = None
        message = getattr(call, "message", None)
        user = getattr(call, "from_user", None)
        key = (getattr(getattr(message, "chat", None), "id", None), getattr(user, "id", None))
        with self._conversation(key):
            try:
                self._cleanup()
                if message is None or not self._private(message, getattr(call, "from_user", None)):
                    notice = "Откройте /settings в личном чате."
                    return
                user = call.from_user
                key = (message.chat.id, user.id)
                parts = str(getattr(call, "data", "")).split(":", 2)
                context = SimpleNamespace(
                    chat=message.chat, from_user=user, message_id=message.message_id, text=None
                )
                if len(parts) == 3 and parts[0] == "confirm":
                    notice = self._confirm(context, parts[1], parts[2])
                    return
                with self._lock:
                    menu = self._menus.get(key)
                if (
                    menu is None or len(parts) != 3 or parts[0] != "admin"
                    or not secrets.compare_digest(parts[1], menu.nonce)
                    or message.message_id != menu.message_id
                    or parts[2] not in _ACTIONS
                ):
                    notice = "Это меню устарело. Откройте /settings."
                    return
                self._pop_state(key)
                self._begin_action(context, parts[2])
            except Exception as error:  # noqa: BLE001 - callback boundary protects polling.
                logger.warning("Callback operation failed (%s)", type(error).__name__)
                notice = "Не удалось выполнить операцию."
            finally:
                try:
                    self.bot.answer_callback_query(call.id, text=notice)
                except Exception as error:  # noqa: BLE001 - acknowledgment failure is isolated.
                    logger.warning("Callback acknowledgment failed (%s)", type(error).__name__)

    def _begin_action(self, message, action):
        admin = self._admin(message, superadmin=action in _SUPERADMIN_ACTIONS)
        if admin is None:
            return
        if action in _SETTING_ACTIONS:
            prompt = "Введите @username или ссылку https://t.me/username. Отмена: /cancel."
        elif action in {"add_admin", "add_superadmin"}:
            prompt = (
                "Введите Telegram ID, логин и пароль нового администратора через пробел.\n"
                "Пример: 123456789 manager длинный пароль\n"
                "Пароль: не менее 12 символов. Отмена: /cancel."
            )
        elif action == "remove_admin":
            prompt = "Введите логин администратора для удаления. Отмена: /cancel."
        elif action == "send_all":
            prompt = "Введите текст рассылки (1–4096 символов). Отмена: /cancel."
        elif action == "post_channel":
            if not self.settings.post_chat_ids:
                self._send(message.chat.id, "Каналы публикации не настроены.")
                return
            prompt = "Введите текст публикации (1–4096 символов). Отмена: /cancel."
        elif action == "import_clients":
            prompt = "Отправьте CSV-файл UTF-8, не более 5 МБ. Отмена: /cancel."
        else:
            self._immediate_action(message, action, admin)
            return
        self._set_state(message, action)
        self._send(message.chat.id, prompt)

    def _immediate_action(self, message, action, admin):
        if action == "get_settings":
            values = self.repository.get_settings()
            lines = [f"{name}: {value}" for name, value in sorted(values.items())]
            if not lines:
                lines.append("Настройки пока не заданы.")
            targets = ", ".join(map(str, self.settings.post_chat_ids)) or "не настроены"
            lines.append(f"Адресаты публикаций (POST_CHAT_IDS): {targets}")
            lines.append("Ссылки в кнопках не меняют адресатов публикаций.")
            self._send_chunks(message.chat.id, "\n".join(lines))
        elif action == "check_admin":
            role = "суперадминистратор" if admin.is_superadmin else "администратор"
            self._send(message.chat.id, f"Ваш доступ: {role}.")
        elif action == "get_admins":
            admins = self.repository.list_admins()
            lines = [
                f"{item.username} · ID {item.telegram_id or 'не привязан'} · "
                f"{'суперадминистратор' if item.is_superadmin else 'администратор'}"
                for item in admins
            ]
            self._send_chunks(message.chat.id, "\n".join(lines) if lines else "Администраторов нет.")
        elif action == "export_clients":
            document = io.BytesIO(self.repository.export_clients())
            document.name = "clients.csv"
            self.bot.send_document(message.chat.id, document, caption="Список клиентов.")
        self._show_menu(message)

    def _send_chunks(self, chat_id, text):
        for offset in range(0, len(text), MAX_TEXT_LENGTH):
            self._send(chat_id, text[offset : offset + MAX_TEXT_LENGTH])

    def _delete_credentials(self, message):
        try:
            self.bot.delete_message(message.chat.id, message.message_id)
        except Exception as error:  # noqa: BLE001 - deletion failure must not expose credentials.
            logger.info("Credential message cleanup failed (%s)", type(error).__name__)

    def _login(self, message, *, username=None):
        text = getattr(message, "text", None)
        if not isinstance(text, str):
            self._send(message.chat.id, "Нужно текстовое сообщение. Отмена: /cancel.")
            return
        self._delete_credentials(message)
        if username is None:
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                self._set_state(message, "login_password", username=parts[0])
                self._send(message.chat.id, "Введите пароль отдельным сообщением. Пробелы в начале и конце сохраняются. Отмена: /cancel.")
                return
            if len(parts) != 2:
                self._send(message.chat.id, "Введите логин либо логин и пароль через пробел. Отмена: /cancel.")
                return
            username, password = parts
        else:
            password = text
        now = self.clock()
        user_id = message.from_user.id
        with self._lock:
            attempts = self._attempts[user_id]
            limited = len(attempts) >= self.settings.auth_max_attempts
            if not limited:
                attempts.append(now)
        if limited:
            self._send(message.chat.id, "Слишком много попыток входа. Попробуйте позже.")
            return
        # Outsiders retain their per-ID limit but cannot consume administrators' quota.
        bound_admin = self.repository.get_admin_by_telegram_id(user_id)
        admin = None
        if bound_admin is not None:
            with self._lock:
                limited = len(self._global_attempts) >= self.settings.auth_global_max_attempts
                if not limited:
                    self._global_attempts.append(now)
            if limited:
                self._send(message.chat.id, "Слишком много попыток входа. Попробуйте позже.")
                return
            admin = self.repository.authenticate_admin(username, password, user_id)
            # Try the exact secret first: existing passwords may legitimately end in CR/LF.
            copied_password = password.rstrip("\r\n")
            if admin is None and copied_password != password:
                admin = self.repository.authenticate_admin(username, copied_password, user_id)
        if admin is None:
            self._send(message.chat.id, "Неверные данные или аккаунт не разрешён для вашего Telegram ID.")
            return
        key = self._key(message)
        with self._lock:
            self._states.pop(key, None)
            self._sessions[key] = AuthSession(now + self.settings.auth_ttl_seconds, admin.auth_revision)
        self._show_menu(message, admin)

    def _process_state(self, message, state):
        if state.action in {"login", "login_password"}:
            self._login(message, username=state.username)
            return
        admin = self._admin(message, superadmin=state.action in _SUPERADMIN_ACTIONS)
        if admin is None:
            return
        if state.action == "import_clients":
            self._import(message)
            return
        text = getattr(message, "text", None)
        if not isinstance(text, str) or not text.strip():
            self._send(message.chat.id, "Нужно текстовое сообщение. Отмена: /cancel.")
            return
        if state.action in _SETTING_ACTIONS:
            try:
                value = normalize_telegram_url(text)
            except ValueError as error:
                self._send(message.chat.id, str(error))
                return
            self.repository.set_setting(
                _SETTING_ACTIONS[state.action], value, actor_id=message.from_user.id,
                expected_auth_revision=admin.auth_revision,
            )
            self._send(message.chat.id, "Настройка сохранена.")
        elif state.action in {"add_admin", "add_superadmin"}:
            self._delete_credentials(message)
            parts = text.split(maxsplit=2)
            if (
                len(parts) != 3 or not parts[0].isascii() or not parts[0].isdigit()
                or int(parts[0]) <= 0 or int(parts[0]) >= 2**52
                or not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", parts[1])
                or len(parts[2]) < 12
            ):
                self._send(
                    message.chat.id,
                    "Нужны положительный Telegram ID, логин (3–50 латинских символов, цифр, _.-) "
                    "и пароль не короче 12 символов.",
                )
                return
            try:
                self.repository.add_admin(
                    parts[1], parts[2], telegram_id=int(parts[0]),
                    is_superadmin=state.action == "add_superadmin",
                    actor_id=message.from_user.id, expected_auth_revision=admin.auth_revision,
                )
            except ValueError:
                self._send(message.chat.id, "Не удалось добавить администратора: проверьте логин и Telegram ID.")
                return
            self._send(message.chat.id, "Администратор добавлен.")
        elif state.action == "remove_admin":
            try:
                removed = self.repository.remove_admin(
                    text.strip(), actor_id=message.from_user.id,
                    expected_auth_revision=admin.auth_revision,
                )
            except ValueError:
                self._send(message.chat.id, "Нельзя удалить эту запись: проверьте логин, права и наличие другого суперадминистратора.")
                return
            self._send(message.chat.id, "Администратор удалён." if removed else "Администратор не найден.")
        elif state.action in {"send_all", "post_channel"}:
            if len(text) > MAX_TEXT_LENGTH:
                self._send(message.chat.id, "Текст слишком длинный: максимум 4096 символов.")
                return
            self._preview(message, state.action, text, admin)
            return
        self._pop_state(self._key(message))
        self._show_menu(message)

    def _import(self, message):
        document = getattr(message, "document", None)
        if document is None or not str(getattr(document, "file_name", "")).lower().endswith(".csv"):
            self._send(message.chat.id, "Пришлите файл с расширением .csv. Отмена: /cancel.")
            return
        size = getattr(document, "file_size", None)
        if size is None or size <= 0 or size > MAX_IMPORT_BYTES:
            self._send(message.chat.id, "Размер CSV должен быть от 1 байта до 5 МБ.")
            return
        file_info = self.bot.get_file(document.file_id)
        content = self.bot.download_file(file_info.file_path)
        if not isinstance(content, bytes) or len(content) > MAX_IMPORT_BYTES:
            self._send(message.chat.id, "Файл превышает допустимый размер.")
            return
        admin = self._admin(message)
        if admin is None:
            return
        try:
            result = self.repository.import_clients(
                content, actor_id=message.from_user.id, expected_auth_revision=admin.auth_revision
            )
        except ValueError:
            self._send(message.chat.id, "CSV не принят. Проверьте UTF-8, заголовки и значения всех строк.")
            return
        self._pop_state(self._key(message))
        self._send(
            message.chat.id,
            f"Импорт завершён. Добавлено: {result.inserted}; дубликатов: {result.duplicates}; строк: {result.total}.",
        )
        self._show_menu(message)

    def _validate_post_targets(self, configured_targets):
        if not configured_targets:
            raise ValueError("Адресаты публикаций не настроены (POST_CHAT_IDS).")
        try:
            bot_id = self.bot.get_me().id
        except Exception:
            raise ValueError("Не удалось проверить доступ бота. Попробуйте позже.") from None
        targets = []
        labels = []
        for configured in configured_targets:
            try:
                chat = self.bot.get_chat(configured)
                member = self.bot.get_chat_member(chat.id, bot_id)
            except Exception:
                raise ValueError(f"Адресат {configured} недоступен для проверки. Проверьте POST_CHAT_IDS и права бота.") from None
            status = getattr(member, "status", None)
            allowed = status == "creator"
            if chat.type == "channel":
                allowed = allowed or (status == "administrator" and getattr(member, "can_post_messages", False) is True)
            elif chat.type in {"group", "supergroup"}:
                permissions = getattr(chat, "permissions", None)
                allowed = allowed or status == "administrator" or (
                    status == "member" and getattr(permissions, "can_send_messages", False) is True
                ) or (
                    status == "restricted" and getattr(member, "is_member", False) is True
                    and getattr(member, "can_send_messages", False) is True
                )
            else:
                allowed = False
            if not allowed:
                raise ValueError(f"Бот не имеет подтверждённого права публиковать в {configured}. Проверьте права и POST_CHAT_IDS.")
            if chat.id not in targets:
                targets.append(chat.id)
                labels.append(f"{configured} → {chat.id}")
        return tuple(targets), labels

    def _preview(self, message, action, text, admin):
        targets = ()
        configured_targets = ()
        contacts = None
        if action == "post_channel":
            configured_targets = tuple(dict.fromkeys(self.settings.post_chat_ids))
            try:
                targets, labels = self._validate_post_targets(configured_targets)
            except ValueError as error:
                self._send_chunks(message.chat.id, str(error))
                return
            contacts = dict(self.repository.get_settings())
            heading = "Предпросмотр публикации. Адресаты:\n" + "\n".join(labels)
            preview_text = text
        else:
            heading = "Предпросмотр рассылки. Получатели: все подписанные клиенты на момент подтверждения. Пример сообщения:"
            preview_text = self._personalize("Имя", text)
        admin = self._admin(message)
        if admin is None:
            return
        key = self._key(message)
        self._clear(key)
        nonce = secrets.token_urlsafe(12)
        self._send_chunks(message.chat.id, heading)
        self._send(message.chat.id, preview_text, reply_markup=generate_contact_keyboard(contacts) if contacts is not None else None)
        sent = self._send(
            message.chat.id, "Подтвердите отправку. /cancel — отменить этот предпросмотр.",
            reply_markup=generate_confirmation_keyboard(nonce),
        )
        with self._lock:
            session = self._sessions.get(key)
            self._confirmations[key] = Confirmation(
                nonce=nonce, message_id=sent.message_id,
                expires=min(self.clock() + self.settings.state_ttl_seconds, session.expires if session else 0),
                action=action, text=text, auth_revision=admin.auth_revision,
                targets=targets, configured_targets=configured_targets, contact_settings=contacts,
            )

    def _confirm(self, message, nonce, decision):
        key = self._key(message)
        with self._lock:
            confirmation = self._confirmations.get(key)
        if (
            confirmation is None or decision not in {"send", "cancel"}
            or not secrets.compare_digest(nonce, confirmation.nonce)
            or confirmation.message_id != message.message_id
        ):
            return "Этот предпросмотр устарел или уже обработан."
        # Consume before I/O: duplicate clicks and ambiguous transport/commit errors cannot repeat it.
        with self._lock:
            self._confirmations.pop(key, None)
        admin = self._admin(message)
        if admin is None or not secrets.compare_digest(confirmation.auth_revision, admin.auth_revision):
            return "Войдите снова и создайте новый предпросмотр."
        if decision == "cancel":
            self._show_menu(message, admin)
            return "Предпросмотр отменён."
        if confirmation.action == "send_all":
            job_id = self.repository.create_broadcast(
                message.from_user.id, confirmation.text, expected_auth_revision=admin.auth_revision,
            )
            self._wake.set()
            self._send(
                message.chat.id,
                f"Рассылка №{job_id} поставлена в очередь.\nСтатус: /broadcast_status {job_id}\nОстановить: /broadcast_cancel {job_id}",
            )
        else:
            try:
                configured = tuple(dict.fromkeys(self.settings.post_chat_ids))
                if configured != confirmation.configured_targets:
                    raise ValueError("Адресаты публикации изменились. Создайте новый предпросмотр.")
                targets, _ = self._validate_post_targets(configured)
                if targets != confirmation.targets:
                    raise ValueError("Адресаты публикации изменились. Создайте новый предпросмотр.")
            except ValueError as error:
                self._send_chunks(message.chat.id, str(error))
                return "Публикация отменена. Создайте новый предпросмотр."
            self._post(message, confirmation.text, targets=targets, contact_settings=confirmation.contact_settings)
        self._show_menu(message)
        return None

    def _post(self, message, text, *, targets, contact_settings):
        markup = generate_contact_keyboard(contact_settings)
        results = []
        for chat_id in targets:
            if self._admin(message) is None:
                break
            status, _, _ = self._deliver(chat_id, text, reply_markup=markup)
            labels = {"sent": "отправлено", "failed": "не отправлено", "pending": "отклонено: лимит Telegram", "uncertain": "результат неизвестен"}
            results.append(f"{chat_id}: {labels[status]}")
        self._send_chunks(message.chat.id, "Результат публикации:\n" + "\n".join(results))

    @staticmethod
    def _personalize(first_name, text):
        personalized = f"Привет, {first_name or 'друг'}!\n{text}"
        return personalized if len(personalized) <= MAX_TEXT_LENGTH else text

    def _deliver(self, chat_id, text, **kwargs):
        try:
            self._send(chat_id, text, **kwargs)
            return "sent", None, None
        except apihelper.ApiTelegramException as error:
            code = getattr(error, "error_code", None)
            if code == 429:
                parameters = (getattr(error, "result_json", None) or {}).get("parameters") or {}
                try:
                    retry_after = max(1, min(float(parameters.get("retry_after", 60)), 86400))
                except (TypeError, ValueError):
                    retry_after = 60
                return "pending", "telegram_429", retry_after
            return ("uncertain" if code is None or code >= 500 else "failed"), f"telegram_{code}", None
        except Exception as error:  # noqa: BLE001 - an unknown transport result must never be retried.
            # The server may have accepted the message before the connection failed.
            return "uncertain", f"send_{type(error).__name__}", None

    def _broadcast_status(self, message):
        admin = self._admin(message)
        if admin is None:
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
            self._send(message.chat.id, "Используйте /broadcast_status НОМЕР.")
            return
        try:
            status = self.repository.get_broadcast_status(int(parts[1]))
        except (ValueError, LookupError):
            status = None
        if not status or (status["actor_id"] != message.from_user.id and not admin.is_superadmin):
            self._send(message.chat.id, "Рассылка не найдена или недоступна.")
            return
        self._send(message.chat.id, self._status_text(status))

    def _broadcast_cancel(self, message):
        admin = self._admin(message)
        if admin is None:
            return
        parts = (message.text or "").split(maxsplit=1)
        number = parts[1].strip() if len(parts) == 2 else ""
        if not number.isascii() or not number.isdigit():
            self._send(message.chat.id, "Используйте /broadcast_cancel НОМЕР.")
            return
        try:
            canceled = self.repository.request_cancel_broadcast(
                int(number), actor_id=message.from_user.id, expected_auth_revision=admin.auth_revision,
            )
        except (ValueError, LookupError, PermissionError):
            self._send(message.chat.id, "Рассылка не найдена или недоступна.")
            return
        self._send(
            message.chat.id,
            "Рассылка остановлена. Уже отправленные и выполняющиеся отправки отозвать нельзя."
            if canceled else "Рассылка уже завершена или отменена.",
        )

    @staticmethod
    def _status_text(status):
        return (
            f"Рассылка №{status['id']} ({status['status']}):\n"
            f"Ожидают: {status['pending']}; выполняются: {status['sending']}; "
            f"отправлены: {status['sent']}; ошибки: {status['failed']}; "
            f"результат неизвестен: {status['uncertain']}; отменены: {status.get('canceled', 0)}."
        )

    def process_broadcast_once(self):
        """Process at most one durable recipient, including in deterministic offline tests."""
        with self._broadcast_lock:
            try:
                result = self._process_broadcast_once()
            except Exception:
                self.worker_healthy = False
                raise
            self.last_worker_progress = self.clock()
            self.worker_healthy = True
            return result

    def _process_broadcast_once(self):
        if self._pending_delivery is not None:
            self._persist_delivery()
            return True
        now = self.clock()
        if now < self._next_send_at:
            return False
        for job in self.repository.pending_broadcasts():
            if self.repository.get_admin_by_telegram_id(job["actor_id"]) is None:
                self.repository.cancel_broadcast(job["id"])
                continue
            recipients = self.repository.get_broadcast_batch(job["id"], limit=1)
            if not recipients:
                continue
            recipient = recipients[0]
            if self.repository.get_admin_by_telegram_id(job["actor_id"]) is None:
                self.repository.record_broadcast_result(
                    job["id"], recipient["chat_id"], status="failed", error="actor_revoked",
                    attempt=recipient["attempt"],
                )
                self.repository.cancel_broadcast(job["id"])
                continue
            if not self.repository.is_client_subscribed(recipient["chat_id"]):
                self._pending_delivery = PendingDelivery(
                    job["id"], job["actor_id"], recipient["chat_id"], "canceled", "client_unsubscribed", None,
                    recipient["attempt"],
                )
                self._persist_delivery()
                return True
            status, error, retry_after = self._deliver(
                recipient["chat_id"], self._personalize(recipient["first_name"], job["text"])
            )
            self._pending_delivery = PendingDelivery(
                job["id"], job["actor_id"], recipient["chat_id"], status, error, retry_after,
                recipient["attempt"],
            )
            self._next_send_at = self.clock() + max(
                self.settings.broadcast_interval_seconds, retry_after or 0
            )
            self._persist_delivery()
            return True
        return False

    def _persist_delivery(self):
        outcome = self._pending_delivery
        self.repository.record_broadcast_result(
            outcome.job_id, outcome.chat_id, status=outcome.status, error=outcome.error,
            retry_after=outcome.retry_after, attempt=outcome.attempt,
        )
        self._pending_delivery = None
        progress = self.repository.get_broadcast_status(outcome.job_id)
        if progress["pending"] == 0 and progress["sending"] == 0 and outcome.job_id not in self._notified_jobs:
            self._notified_jobs.append(outcome.job_id)
            self._safe_notice(outcome.actor_id, self._status_text(progress))

    def _work(self):
        while not self._stop.is_set():
            try:
                worked = self.process_broadcast_once()
            except Exception as error:  # noqa: BLE001 - keep the worker alive; pending claims stay durable.
                logger.warning("Broadcast worker operation failed (%s)", type(error).__name__)
                worked = False
            if not worked:
                self._wake.wait(0.5)
                self._wake.clear()
            else:
                self._stop.wait(self.settings.broadcast_interval_seconds)

    def start(self):
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self.repository.recover_broadcasts()
            self._stop.clear()
            self.last_worker_progress = self.clock()
            self.worker_healthy = True
            self._worker = threading.Thread(target=self._work, name="broadcast-worker", daemon=True)
            self._worker.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=10)


def setup_bot_handlers(bot, repository=None, settings=None):
    if settings is None:
        from config import load_settings
        settings = load_settings(require_token=False)
    if repository is None:
        from db import Repository
        repository = Repository(settings.database_url, backup_dir=settings.backup_dir)
        repository.initialize()
    return BotApplication(bot, repository, settings)
