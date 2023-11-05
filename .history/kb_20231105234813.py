from telebot import types
import os

def generate_contact_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    manager_username = os.getenv('MANAGER_USERNAME')
    deposit_button = types.InlineKeyboardButton(text="💰 Внести депозит", url="https://t.me/manager_username")
    contact_button = types.InlineKeyboardButton(text="🤝 Связаться с менеджером", url="https://t.me/manager_username")
    channel_button = types.InlineKeyboardButton(text="📢 Перейти в канал", url="https://t.me/+E6Xo5rKob8k4MzIy")
    chat_button = types.InlineKeyboardButton(text="💬 Перейти в чат", url="@HappyWeekendVip_bot")

    # Добавляем каждую кнопку в отдельный ряд
    keyboard.row(deposit_button)
    keyboard.row(contact_button)
    keyboard.row(channel_button)
    keyboard.row(chat_button)
    return keyboard
