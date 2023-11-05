from telebot import types
import os

def generate_contact_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    manager_username = os.getenv('MANAGER_USERNAME')
    buttons = [
            types.InlineKeyboardButton(text="💰 Внести депозит", url=f"https://t.me/{manager_username}"),
            types.InlineKeyboardButton(text="🤝 Связаться с менеджером", url=f"https://t.me/{manager_username}"),
            types.InlineKeyboardButton(text="📢 Перейти в канал", url="https://t.me/+E6Xo5rKob8k4MzIy"),
            types.InlineKeyboardButton(text="💬 Перейти в чат", url="https://t.me/HappyWeekendVip_bot")

    ]
    keyboard.add(*buttons)
    return keyboard
