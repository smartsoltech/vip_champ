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
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add('Изменить контакт менеджера'; 'Изменить ссылку на канал'; 'Изменить ссылку на бота')
    if os.getenv('IS_SUPERADMIN') == 'true':  # Предполагаем, что у вас есть переменная окружения IS_SUPERADMIN
        keyboard.add('Добавить админа')
    return keyboard