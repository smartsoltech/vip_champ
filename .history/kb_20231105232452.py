from telebot import types
import os

def generate_contact_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    manager_username = os.getenv('MANAGER_USERNAME')
    contact_button = types.InlineKeyboardButton(text="Связаться с менеджером", url=f"https://t.me/{manager_username}")
    keyboard.add(contact_button)
    return keyboard
