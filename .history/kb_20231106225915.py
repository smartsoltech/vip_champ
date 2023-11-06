from telebot import types
import os

def generate_contact_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    manager_username = os.getenv('MANAGER_USERNAME')
    deposit_button = types.InlineKeyboardButton(text="💰 Внести депозит", url="https://t.me/VIPchampKR")
    contact_button = types.InlineKeyboardButton(text="🤝 Связаться с менеджером", url="https://t.me/VIPchampKR")
    channel_button = types.InlineKeyboardButton(text="📢 Перейти в канал", url="https://t.me/+E6Xo5rKob8k4MzIy")
    chat_button = types.InlineKeyboardButton(text="💬 Перейти в чат розыгрыша", url="https://t.me/HappyWeekendVip_bot")

    # Добавляем каждую кнопку в отдельный ряд
    keyboard.row(deposit_button)
    keyboard.row(contact_button)
    keyboard.row(channel_button)
    keyboard.row(chat_button)
    return keyboard

def generate_admin_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton('Изменить контакт менеджера', callback_data='change_manager'))
    keyboard.add(types.InlineKeyboardButton('Изменить ссылку на канал', callback_data='change_channel'))
    keyboard.add(types.InlineKeyboardButton('Изменить ссылку на бота', callback_data='change_bot'))
    return keyboard