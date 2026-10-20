"""Telegram keyboards and contact validation. No database or environment side effects."""

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from telebot import types

_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
_INVITE = re.compile(r"(?:\+|joinchat/)[A-Za-z0-9_-]{8,128}\Z")


def normalize_telegram_url(value: str) -> str:
    """Accept a Telegram username or an HTTPS Telegram/invitation link."""
    if not isinstance(value, str):
        raise TypeError("Укажите Telegram username или ссылку https://t.me/username.")
    value = value.strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("Ссылка не должна быть пустой или содержать пробелы.")
    if value.startswith("@"):
        path = value[1:]
    elif value.startswith(("t.me/", "telegram.me/")):
        path = value.split("/", 1)[1]
    elif "://" in value:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() not in {"t.me", "telegram.me"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Разрешены ссылки https://t.me/username без дополнительных параметров.")
        path = parsed.path.removeprefix("/").removesuffix("/")
    else:
        path = value
    if not (_USERNAME.fullmatch(path) or _INVITE.fullmatch(path)):
        raise ValueError("Укажите корректный Telegram username или ссылку-приглашение.")
    return f"https://t.me/{path}"


def generate_contact_keyboard(settings: Mapping[str, str] | None = None):
    settings = settings or {}
    keyboard = types.InlineKeyboardMarkup()
    buttons = (
        ("manager_contact", "💰 Внести депозит"),
        ("manager_contact", "🤝 Связаться с менеджером"),
        ("channel_link", "📢 Перейти в канал"),
        ("bot_link", "💬 Перейти в чат розыгрыша"),
    )
    count = 0
    for key, label in buttons:
        try:
            url = normalize_telegram_url(settings.get(key))
        except (TypeError, ValueError):
            continue
        keyboard.row(types.InlineKeyboardButton(text=label, url=url))
        count += 1
    return keyboard if count else None


def generate_admin_inline_keyboard(nonce: str, is_superadmin: bool = False):
    keyboard = types.InlineKeyboardMarkup()
    actions = [
        ("📞 Контакт менеджера", "change_manager_contact"),
        ("🔗 Ссылка на канал", "change_channel_link"),
        ("💬 Ссылка на чат", "change_bot_link"),
        ("⚙️ Текущие настройки", "get_settings"),
        ("📁 Экспорт клиентов", "export_clients"),
        ("📥 Импорт клиентов", "import_clients"),
        ("📨 Рассылка", "send_all"),
        ("📢 Публикация", "post_channel"),
        ("🔎 Мой доступ", "check_admin"),
    ]
    if is_superadmin:
        actions.extend(
            [
                ("👥 Администраторы", "get_admins"),
                ("➕ Добавить админа", "add_admin"),
                ("➕ Добавить суперадмина", "add_superadmin"),
                ("➖ Удалить админа", "remove_admin"),
            ]
        )
    for index in range(0, len(actions), 2):
        keyboard.row(
            *[
                types.InlineKeyboardButton(text=label, callback_data=f"admin:{nonce}:{action}")
                for label, action in actions[index : index + 2]
            ]
        )
    return keyboard


def generate_admin_keyboard(is_su=False):
    """Compatibility helper; commands use the same authorization as inline actions."""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add("/settings", "/cancel")
    if is_su:
        keyboard.add("/add_admin", "/add_superadmin")
    return keyboard


def generate_confirmation_keyboard(nonce: str):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("Подтвердить отправку", callback_data=f"confirm:{nonce}:send"),
        types.InlineKeyboardButton("Отменить", callback_data=f"confirm:{nonce}:cancel"),
    )
    return keyboard
