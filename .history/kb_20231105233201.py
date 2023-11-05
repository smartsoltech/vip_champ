from telebot import types
import os

def generate_contact_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    manager_username = os.getenv('MANAGER_USERNAME')
    buttons = [
        types.InlineKeyboardButton(text="Внести депозит", url=f"https://t.me/{manager_username}"),
        types.InlineKeyboardButton(text="Связаться с менеджером", url=f"https://t.me/{manager_username}"),
        types.InlineKeyboardButton(text="Внести депозит", url=f"https://t.me/+E6Xo5rKob8k4MzIy"),
        types.InlineKeyboardButton(text="Связаться с менеджером", url=f"https://t.me/{manager_username}")
    ]
    contact_button = 
    keyboard.add(contact_button)
    return keyboard
